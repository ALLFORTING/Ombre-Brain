# ============================================================
# Module: Memory Import Engine (import_memory.py)
# 模块：历史记忆导入引擎
#
# Imports conversation history from various platforms into OB.
# 将各平台对话历史导入 OB 记忆系统。
#
# Supports: Claude JSON, ChatGPT export, DeepSeek, Markdown, plain text
# 支持格式：Claude JSON、ChatGPT 导出、DeepSeek、Markdown、纯文本
#
# Features:
#   - Chunked processing with resume support
#   - Progress persistence (import_state.json)
#   - Raw preservation mode for special contexts
#   - Post-import frequency pattern detection
# ============================================================

import os
import asyncio
import copy
import inspect
import sqlite3
import sys
import tempfile
import time
from uuid import uuid4
import json
import hashlib
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import DISPLAY_ALIASES, apply_display_aliases_to_value, count_tokens_approx, now_iso
from bucket_manager import (
    BucketManager,
    BucketIdempotencyError,
    automatic_todo_provenance,
    canonicalize_todos,
    merge_todo_provenance,
    reconcile_todo_provenance,
)
from bucket_write_lock import bucket_write_scope
from maintenance_write_gate import guarded_mutation, guarded_async_mutation, MaintenanceWriteError

logger = logging.getLogger("ombre_brain.import")
_LEGACY_RUN_LOCKS = {}
_LEGACY_LEASE_SECONDS = 60.0
_IMPORT_PUBLIC_FIELDS = (
    "source_file", "source_hash", "total_chunks", "processed", "api_calls",
    "memories_created", "memories_merged", "memories_raw", "errors", "status",
    "started_at", "updated_at",
)


# ============================================================
# Format Parsers — normalize any format to conversation turns
# 格式解析器 — 将任意格式标准化为对话轮次
# ============================================================

def _parse_claude_json(data: dict | list) -> list[dict]:
    """Parse Claude.ai export JSON → [{role, content, timestamp}, ...]"""
    turns = []
    conversations = data if isinstance(data, list) else [data]
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        messages = conv.get("chat_messages", conv.get("messages", []))
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            content = msg.get("text", msg.get("content", ""))
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            if not content or not content.strip():
                continue
            role = msg.get("sender", msg.get("role", "user"))
            ts = msg.get("created_at", msg.get("timestamp", ""))
            turns.append({"role": role, "content": content.strip(), "timestamp": ts})
    return turns


def _parse_chatgpt_json(data: list | dict) -> list[dict]:
    """Parse ChatGPT export JSON → [{role, content, timestamp}, ...]"""
    turns = []
    conversations = data if isinstance(data, list) else [data]
    for conv in conversations:
        if not isinstance(conv, dict):
            continue
        mapping = conv.get("mapping", {})
        if mapping:
            # ChatGPT uses a tree structure with mapping
            # Filter out None nodes before sorting
            valid_nodes = [n for n in mapping.values() if isinstance(n, dict)]

            def _node_ts(n):
                msg = n.get("message")
                if not isinstance(msg, dict):
                    return 0
                return msg.get("create_time") or 0

            sorted_nodes = sorted(valid_nodes, key=_node_ts)
            for node in sorted_nodes:
                msg = node.get("message")
                if not msg or not isinstance(msg, dict):
                    continue
                content_obj = msg.get("content", {})
                content_parts = content_obj.get("parts", []) if isinstance(content_obj, dict) else []
                content = " ".join(str(p) for p in content_parts if p)
                if not content.strip():
                    continue
                role = msg.get("author", {}).get("role", "user")
                ts = msg.get("create_time", "")
                if isinstance(ts, (int, float)):
                    ts = datetime.fromtimestamp(ts).isoformat()
                turns.append({"role": role, "content": content.strip(), "timestamp": str(ts)})
        else:
            # Simpler format: list of messages
            messages = conv.get("messages", [])
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", msg.get("text", ""))
                if isinstance(content, dict):
                    content = " ".join(str(p) for p in content.get("parts", []))
                if not content or not content.strip():
                    continue
                role = msg.get("role", msg.get("author", {}).get("role", "user"))
                ts = msg.get("timestamp", msg.get("create_time", ""))
                turns.append({"role": role, "content": content.strip(), "timestamp": str(ts)})
    return turns


def _parse_markdown(text: str) -> list[dict]:
    """Parse Markdown/plain text → [{role, content, timestamp}, ...]"""
    # Try to detect conversation patterns
    lines = text.split("\n")
    turns = []
    current_role = "user"
    current_content = []

    for line in lines:
        stripped = line.strip()
        # Detect role switches
        if stripped.lower().startswith(("human:", "user:", "你:", "我:")):
            if current_content:
                turns.append({"role": current_role, "content": "\n".join(current_content).strip(), "timestamp": ""})
            current_role = "user"
            content_after = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            current_content = [content_after] if content_after else []
        elif stripped.lower().startswith(("assistant:", "claude:", "ai:", "gpt:", "bot:", "deepseek:")):
            if current_content:
                turns.append({"role": current_role, "content": "\n".join(current_content).strip(), "timestamp": ""})
            current_role = "assistant"
            content_after = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
            current_content = [content_after] if content_after else []
        else:
            current_content.append(line)

    if current_content:
        content = "\n".join(current_content).strip()
        if content:
            turns.append({"role": current_role, "content": content, "timestamp": ""})

    # If no role patterns detected, treat entire text as one big chunk
    if not turns:
        turns = [{"role": "user", "content": text.strip(), "timestamp": ""}]

    return turns


def detect_and_parse(raw_content: str, filename: str = "") -> list[dict]:
    """
    Auto-detect format and parse to normalized turns.
    自动检测格式并解析为标准化的对话轮次。
    """
    ext = Path(filename).suffix.lower() if filename else ""

    # Try JSON first
    if ext in (".json", "") or raw_content.strip().startswith(("{", "[")):
        try:
            data = json.loads(raw_content)
            # Detect Claude vs ChatGPT format
            if isinstance(data, list):
                sample = data[0] if data else {}
            else:
                sample = data

            if isinstance(sample, dict):
                if "chat_messages" in sample:
                    return _parse_claude_json(data)
                if "mapping" in sample:
                    return _parse_chatgpt_json(data)
                if "messages" in sample:
                    # Could be either — try ChatGPT first, fall back to Claude
                    msgs = sample["messages"]
                    if msgs and isinstance(msgs[0], dict) and "content" in msgs[0]:
                        if isinstance(msgs[0]["content"], dict):
                            return _parse_chatgpt_json(data)
                    return _parse_claude_json(data)
                # Single conversation object with role/content messages
                if "role" in sample and "content" in sample:
                    return _parse_claude_json(data)
        except (json.JSONDecodeError, KeyError, IndexError, AttributeError, TypeError):
            pass

    # Fall back to markdown/text
    return _parse_markdown(raw_content)


# ============================================================
# Chunking — split turns into ~10k token windows
# 分窗 — 按对话轮次边界切为 ~10k token 窗口
# ============================================================

def chunk_turns(turns: list[dict], target_tokens: int = 10000) -> list[dict]:
    """
    Group conversation turns into chunks of ~target_tokens.
    Returns list of {content, timestamp_start, timestamp_end, turn_count}.
    按对话轮次边界将对话分为 ~target_tokens 大小的窗口。
    """
    chunks = []
    current_lines = []
    current_tokens = 0
    first_ts = ""
    last_ts = ""
    turn_count = 0

    for turn in turns:
        role_label = "用户" if turn["role"] in ("user", "human") else "AI"
        line = f"[{role_label}] {turn['content']}"
        line_tokens = count_tokens_approx(line)

        # If single turn exceeds target, split it
        if line_tokens > target_tokens * 1.5:
            # Flush current
            if current_lines:
                chunks.append({
                    "content": "\n".join(current_lines),
                    "timestamp_start": first_ts,
                    "timestamp_end": last_ts,
                    "turn_count": turn_count,
                })
                current_lines = []
                current_tokens = 0
                turn_count = 0
                first_ts = ""

            # Add oversized turn as its own chunk
            chunks.append({
                "content": line,
                "timestamp_start": turn.get("timestamp", ""),
                "timestamp_end": turn.get("timestamp", ""),
                "turn_count": 1,
            })
            continue

        if current_tokens + line_tokens > target_tokens and current_lines:
            chunks.append({
                "content": "\n".join(current_lines),
                "timestamp_start": first_ts,
                "timestamp_end": last_ts,
                "turn_count": turn_count,
            })
            current_lines = []
            current_tokens = 0
            turn_count = 0
            first_ts = ""

        if not first_ts:
            first_ts = turn.get("timestamp", "")
        last_ts = turn.get("timestamp", "")
        current_lines.append(line)
        current_tokens += line_tokens
        turn_count += 1

    if current_lines:
        chunks.append({
            "content": "\n".join(current_lines),
            "timestamp_start": first_ts,
            "timestamp_end": last_ts,
            "turn_count": turn_count,
        })

    return chunks


# ============================================================
# Import State — persistent progress tracking
# 导入状态 — 持久化进度追踪
# ============================================================

class ImportState:
    """Manages import progress with file-based persistence."""

    def __init__(self, state_dir: str):
        self.state_file = os.path.join(state_dir, "import_state.json")
        self.data = {
            "source_file": "",
            "source_hash": "",
            "total_chunks": 0,
            "processed": 0,
            "api_calls": 0,
            "memories_created": 0,
            "memories_merged": 0,
            "memories_raw": 0,
            "errors": [],
            "status": "idle",  # idle | running | paused | completed | error
            "started_at": "",
            "updated_at": "",
        }

    def load(self) -> bool:
        """Load state from file. Returns True if state exists."""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self.data.update(saved)
                return True
            except (json.JSONDecodeError, OSError):
                return False
        return False

    @guarded_mutation("import_state_write")
    def save(self):
        """Persist state to file."""
        self.data["updated_at"] = now_iso()
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)

    def reset(self, source_file: str, source_hash: str, total_chunks: int):
        """Reset state for a new import."""
        self.data = {
            "source_file": source_file,
            "source_hash": source_hash,
            "total_chunks": total_chunks,
            "processed": 0,
            "api_calls": 0,
            "memories_created": 0,
            "memories_merged": 0,
            "memories_raw": 0,
            "errors": [],
            "status": "running",
            "started_at": now_iso(),
            "updated_at": now_iso(),
        }

    @property
    def can_resume(self) -> bool:
        return self.data["status"] in ("paused", "running") and self.data["processed"] < self.data["total_chunks"]

    def to_dict(self) -> dict:
        return dict(self.data)

    @property
    def root(self):
        return str(Path(self.state_file).parent.resolve())

    def read_legacy(self):
        """Pure read: malformed old bytes are evidence, never a fresh run."""
        try:
            raw = Path(self.state_file).read_bytes()
        except FileNotFoundError:
            return None, None
        try:
            saved = json.loads(raw)
            if not isinstance(saved, dict):
                saved = {}
        except (ValueError, UnicodeError):
            saved = {}
        return saved, raw

    @staticmethod
    def is_v2(saved):
        return bool(saved and saved.get('schema_version') == 2 and saved.get('mode') == 'legacy')

    @staticmethod
    def v1_completed(saved):
        return bool(saved and not saved.get('schema_version') and not saved.get('raw_evidence_capture')
                    and saved.get('status') == 'completed'
                    and type(saved.get('processed')) is int
                    and type(saved.get('total_chunks')) is int
                    and saved['processed'] == saved['total_chunks'] and saved['processed'] >= 0)

    def resume_preflight(self):
        """Must run before lazy runtime construction, locks or DB initialization."""
        saved, raw = self.read_legacy()
        if raw is not None and not self.is_v2(saved) and not self.v1_completed(saved):
            raise BucketIdempotencyError('legacy_resume_unverifiable')
        return saved

    @staticmethod
    def public_status(saved):
        if not saved:
            return {field: ('' if field in ('source_file', 'source_hash', 'started_at', 'updated_at')
                            else [] if field == 'errors' else 'idle' if field == 'status' else 0)
                    for field in _IMPORT_PUBLIC_FIELDS}
        if ImportState.is_v2(saved) and saved.get('status') == 'completed':
            return copy.deepcopy(saved['completed_receipt'])
        status = {field: copy.deepcopy(saved.get(field)) for field in _IMPORT_PUBLIC_FIELDS}
        status['errors'] = ['import_failed'] * len(saved.get('errors', []))
        if saved.get('raw_evidence_capture'):
            status['raw_evidence_capture'] = True
        return status

    @guarded_mutation('legacy_import_v1_snapshot')
    def preserve_v1(self, raw):
        """Publish a digest-named opaque snapshot without replacing any winner."""
        parent = Path(self.state_file).parent
        path = parent / ('import_state.v1.' + hashlib.sha256(raw).hexdigest() + '.json')
        if path.exists():
            if path.read_bytes() != raw:
                raise BucketIdempotencyError('legacy_snapshot_conflict')
            BucketManager._sync_directory(str(parent))
            return
        fd, temporary = tempfile.mkstemp(prefix='.import-state-v1-', suffix='.tmp', dir=parent)
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != raw:
                    raise BucketIdempotencyError('legacy_snapshot_conflict')
            BucketManager._sync_directory(str(parent))
        finally:
            os.unlink(temporary)
            BucketManager._sync_directory(str(parent))

    @staticmethod
    def project_counters(saved):
        chunks = saved['chunks']
        saved['total_chunks'] = len(chunks)
        saved['processed'] = sum(c['status'] == 'completed' for c in chunks)
        saved['api_calls'] = sum(c.get('api_calls', 0) for c in chunks)
        done = [item for chunk in chunks for item in (chunk.get('items') or [])
                if item['status'] == 'completed']
        saved['memories_created'] = sum(item['plan']['decision'] == 'create' for item in done)
        saved['memories_merged'] = sum(item['plan']['decision'] == 'reuse' for item in done)
        saved['memories_raw'] = sum(item['plan']['preserve_raw'] for item in done)

    @guarded_mutation('legacy_import_state_publish')
    def publish(self, saved, *, ownership_only=False):
        if not ownership_only:
            saved['updated_at'] = now_iso()
            self.project_counters(saved)
        if saved['phase'] == 'run.completed' and not ownership_only:
            saved['completed_receipt'] = {
                field: copy.deepcopy(saved[field]) for field in _IMPORT_PUBLIC_FIELDS}
            saved['completed_receipt']['errors'] = ['import_failed'] * len(saved['errors'])
        BucketManager._write_bytes_atomic(self.state_file,
            json.dumps(saved, ensure_ascii=False, indent=2).encode('utf-8'))
        self.data = copy.deepcopy(saved)

    def fence(self, claim):
        saved, _ = self.read_legacy()
        if (not self.is_v2(saved) or saved.get('root_binding') != self.root
                or saved.get('run_id') != claim['run_id'] or saved.get('owner') != claim['owner']
                or saved.get('epoch') != claim['epoch'] or saved.get('lease_until', 0) <= time.time()):
            raise BucketIdempotencyError('operation_claim_stale')
        return saved

    @guarded_mutation('legacy_import_claim')
    def claim(self, run_id, owner):
        with bucket_write_scope(self.root):
            saved, _ = self.read_legacy()
            if (not self.is_v2(saved) or saved['root_binding'] != self.root or saved['run_id'] != run_id):
                raise BucketIdempotencyError('operation_claim_stale')
            if saved['status'] == 'completed':
                return {'replay': self.public_status(saved)}
            if saved.get('owner') and saved['lease_until'] > time.time():
                return None
            saved.update(owner=owner, epoch=saved['epoch'] + 1,
                         lease_until=time.time() + _LEGACY_LEASE_SECONDS, status='running')
            self.publish(saved)
            return {'run_id': run_id, 'owner': owner, 'epoch': saved['epoch']}

    @guarded_mutation('legacy_import_checkpoint')
    def checkpoint(self, claim, phase=None, change=None):
        with bucket_write_scope(self.root):
            saved = self.fence(claim)
            if change is not None:
                change(saved)
            if phase is not None:
                saved['phase'] = phase
            self.publish(saved)
            return saved

    @guarded_mutation('legacy_import_heartbeat')
    def heartbeat(self, claim):
        with bucket_write_scope(self.root):
            saved = self.fence(claim)
            saved['lease_until'] = time.time() + _LEGACY_LEASE_SECONDS
            self.publish(saved, ownership_only=True)

    @guarded_mutation('legacy_import_release')
    def release(self, claim):
        with bucket_write_scope(self.root):
            saved, _ = self.read_legacy()
            if (self.is_v2(saved) and saved['run_id'] == claim['run_id']
                    and saved['owner'] == claim['owner'] and saved['epoch'] == claim['epoch']):
                saved.update(owner=None, lease_until=0)
                if saved['status'] == 'running':
                    saved['status'] = 'paused'
                self.publish(saved, ownership_only=saved['status'] == 'completed')

    @guarded_mutation('legacy_import_pause')
    def request_pause(self):
        with bucket_write_scope(self.root):
            saved, _ = self.read_legacy()
            if self.is_v2(saved) and saved['status'] != 'completed':
                saved['pause_requested'] = True
                self.publish(saved)


# ============================================================
# Import extraction prompt
# 导入提取提示词
# ============================================================

IMPORT_EXTRACT_PROMPT = """你是一个对话记忆提取专家。从以下对话片段中提取值得长期记住的信息。

提取规则：
1. 提取用户的事实、偏好、习惯、重要事件、情感时刻
2. 仅当零散片段明确描述同一底层事实或事件时才整合；仅同一话题不够。不同日期、事件、人物/实体、状态、计划与结果、修正/否定或改变的观点必须分别保留为独立记忆
3. 过滤掉纯技术调试输出、代码块、重复问答、无意义寒暄
4. 如果对话中有特殊暗号、仪式性行为、关键承诺等，标记 preserve_raw=true；这表示保留本次提取结果中的 content，并在导入时跳过后续合并/脱水摘要，不表示保留上传文件的原始逐字稿或来源证据
5. 如果内容是用户和AI之间的习惯性互动模式（例如打招呼方式、告别习惯），标记 is_pattern=true
6. 每条记忆不少于30字
7. 总条目数控制在 0~5 个（没有值得记的就返回空数组）
8. 在 content 中对人名、地名、专有名词用 [[双链]] 标记

输出格式（纯 JSON 数组，无其他内容）：
[
  {
    "name": "条目标题（10字以内）",
    "content": "整理后的内容",
    "domain": ["主题域1"],
    "valence": 0.7,
    "arousal": 0.4,
    "tags": ["核心词1", "核心词2", "扩展词1"],
    "importance": 5,
    "todos": ["明确的未完成事项"],
    "preserve_raw": false,
    "is_pattern": false
  }
]

主题域可选（选 1~2 个）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]

importance: 1-10
todos: 只提取原文明确表达且当前仍未完成的事项；没有就返回空数组，不要推测或创造任务
valence: 0~1（0=消极, 0.5=中性, 1=积极）
arousal: 0~1（0=平静, 0.5=普通, 1=激动）
preserve_raw: true = 保留本次提取结果中的 content，并在导入时跳过后续合并/脱水摘要；不表示保留上传文件原文、精确引文或不可变来源证据
is_pattern: true = 反复出现的习惯性行为模式"""


# ============================================================
# Import Engine — core processing logic
# 导入引擎 — 核心处理逻辑
# ============================================================

class ImportEngine:
    """
    Processes conversation history files into OB memory buckets.
    将对话历史文件处理为 OB 记忆桶。
    """

    def __init__(self, config: dict, bucket_mgr, dehydrator, embedding_engine=None):
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.embedding_engine = embedding_engine
        self.state = ImportState(config["buckets_dir"])
        self._paused = False
        self._running = False
        self._chunks: list[dict] = []

    @property
    def is_running(self) -> bool:
        return self._running

    def pause(self):
        """Request pause — will stop after current chunk finishes."""
        self._paused = True
        saved, _ = self.state.read_legacy()
        if self.state.is_v2(saved):
            self.state.request_pause()

    def get_status(self) -> dict:
        """Get current import status."""
        saved, raw = self.state.read_legacy()
        return self.state.public_status(saved if raw is not None else self.state.data)

    async def start(
        self,
        raw_content: str,
        filename: str = "",
        preserve_raw: bool = False,
        resume: bool = False,
    ) -> dict:
        """
        Start or resume an import.
        开始或恢复导入。
        """
        try:
            accepted = self.accept_legacy(raw_content, filename, preserve_raw, resume)
        except BucketIdempotencyError as exc:
            return {'error': str(exc)}
        return await self.run_legacy(accepted)

    @staticmethod
    def _legacy_digest(value):
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        return hashlib.sha256(serialized.encode('utf-8')).hexdigest()

    def _legacy_pipeline(self):
        functions = (_parse_claude_json, _parse_chatgpt_json, _parse_markdown, detect_and_parse,
                     chunk_turns, ImportEngine._parse_extraction)
        return dict(version=1, parser_chunker_digest=self._legacy_digest(
                        [inspect.getsource(function) for function in functions]),
                    target_tokens=10000, extractor_version=1,
                    prompt_digest=hashlib.sha256(IMPORT_EXTRACT_PROMPT.encode()).hexdigest(),
                    model=getattr(self.dehydrator, 'model', ''), input_chars=12000,
                    max_tokens=2048, temperature=0.0,
                    aliases=[[source, target] for source, target in DISPLAY_ALIASES.items()])

    @staticmethod
    def _legacy_chunk_descriptor(chunk, ordinal):
        return dict(ordinal=ordinal, digest=ImportEngine._legacy_digest(chunk),
                    timestamp_start=chunk.get('timestamp_start', ''),
                    timestamp_end=chunk.get('timestamp_end', ''), turn_count=chunk['turn_count'])

    @guarded_mutation('legacy_import_accept')
    def accept_legacy(self, raw_content, filename='', preserve_raw=False, resume=False):
        """Durably bind one active run before HTTP acknowledges or schedules it."""
        saved = self.state.resume_preflight() if resume else self.state.read_legacy()[0]
        digest = hashlib.sha256(raw_content.encode()).hexdigest()
        if resume and self.state.v1_completed(saved):
            if saved.get('source_hash') != digest[:16]:
                raise BucketIdempotencyError('legacy_source_conflict')
            return {'replay': self.state.public_status(saved)}
        if resume and self.state.is_v2(saved) and saved['status'] == 'completed':
            self._validate_legacy_binding(saved, digest, filename, preserve_raw)
            return {'replay': self.state.public_status(saved)}
        turns = detect_and_parse(raw_content, filename)
        if not turns:
            raise BucketIdempotencyError('No conversation turns found in file')
        chunks = chunk_turns(turns)
        if not chunks:
            raise BucketIdempotencyError('No processable chunks after splitting')
        descriptors = [self._legacy_chunk_descriptor(c, i) for i, c in enumerate(chunks)]
        pipeline = self._legacy_pipeline()
        with bucket_write_scope(self.state.root):
            saved, old_bytes = self.state.read_legacy()
            if resume:
                # Recheck after acquiring the root mutex: another acceptance may have won.
                if not self.state.is_v2(saved):
                    if self.state.v1_completed(saved) and saved.get('source_hash') == digest[:16]:
                        return {'replay': self.state.public_status(saved)}
                    raise BucketIdempotencyError('legacy_resume_unverifiable')
                self._validate_legacy_binding(saved, digest, filename, preserve_raw)
                if saved['status'] == 'completed':
                    return {'replay': self.state.public_status(saved)}
                if (saved['pipeline'] != pipeline or len(descriptors) != len(saved['chunks']) or descriptors != [
                        {key: c[key] for key in descriptors[i]} for i, c in enumerate(saved['chunks'])]):
                    raise BucketIdempotencyError('legacy_pipeline_conflict')
                if not saved.get('owner') or saved['lease_until'] <= time.time():
                    saved['pause_requested'] = False
                    self.state.publish(saved)
            else:
                if self.state.is_v2(saved) and saved['status'] != 'completed':
                    raise BucketIdempotencyError('Import already running')
                if saved and saved.get('raw_evidence_capture') and saved.get('status') != 'completed':
                    raise BucketIdempotencyError('Import already running')
                if self._running:
                    raise BucketIdempotencyError('Import already running')
                try:
                    self.bucket_mgr._ensure_import_operation_table()
                except sqlite3.Error as exc:
                    logger.exception('Legacy import schema upgrade failed')
                    raise BucketIdempotencyError('legacy_import_schema_upgrade_failed') from exc
                if not self.bucket_mgr.legacy_import_receipts_available():
                    raise BucketIdempotencyError('legacy_effect_receipts_unavailable')
                if old_bytes is not None and not self.state.is_v2(saved):
                    self.state.preserve_v1(old_bytes)
                saved = dict(schema_version=2, mode='legacy', run_id=uuid4().hex,
                    root_binding=self.state.root, source_digest=digest, source_hash=digest[:16],
                    source_file=filename, preserve_raw=preserve_raw, pipeline=pipeline,
                    phase='run.accepted', status='running', errors=[], last_error_code=None,
                    started_at=now_iso(), updated_at='', pause_requested=False,
                    owner=None, epoch=0, lease_until=0, completed_receipt=None,
                    chunks=[dict(d, status='extraction_pending', items=None, outcome=None, api_calls=0)
                            for d in descriptors])
                self.state.publish(saved)
        return {'run_id': saved['run_id'], 'chunks': chunks}

    def _validate_legacy_binding(self, saved, digest, filename, preserve_raw):
        if (saved['root_binding'] != self.state.root or saved['source_digest'] != digest
                or saved['source_file'] != filename or saved['preserve_raw'] != preserve_raw):
            raise BucketIdempotencyError('legacy_source_conflict')

    async def _legacy_heartbeat(self, claim):
        while True:
            await asyncio.sleep(_LEGACY_LEASE_SECONDS / 3)
            self.state.heartbeat(claim)

    @guarded_async_mutation('legacy_import_execute')
    async def run_legacy(self, accepted):
        if 'replay' in accepted:
            return accepted['replay']
        key = (os.getpid(), asyncio.get_running_loop(), self.state.root, accepted['run_id'])
        lock = _LEGACY_RUN_LOCKS.setdefault(key, asyncio.Lock())
        async with lock:
            claim = None
            heartbeat = None
            self._running = True
            self._paused = False
            try:
                while (claim := self.state.claim(accepted['run_id'], uuid4().hex)) is None:
                    await asyncio.sleep(.05)
                if 'replay' in claim:
                    return claim['replay']
                heartbeat = asyncio.create_task(self._legacy_heartbeat(claim))
                for ordinal, chunk in enumerate(accepted['chunks']):
                    saved = self.state.fence(claim)
                    if saved['chunks'][ordinal]['status'] == 'completed':
                        continue
                    if saved['pause_requested']:
                        self.state.checkpoint(claim, change=lambda s: s.update(status='paused'))
                        return self.get_status()
                    await self._legacy_process_chunk(claim, ordinal, chunk)
                self.state.checkpoint(claim, 'run.completed', lambda s: s.update(status='completed'))
                return self.get_status()
            except (BucketIdempotencyError, MaintenanceWriteError, OSError):
                # Storage/fencing failures are never ordinary extraction outcomes.
                self._legacy_record_error(claim)
                raise
            except Exception:
                self._legacy_record_error(claim)
                raise
            finally:
                try:
                    if heartbeat is not None:
                        heartbeat.cancel()
                        try:
                            await heartbeat
                        except asyncio.CancelledError:
                            pass
                        except Exception:
                            logger.exception('Legacy import heartbeat failed')
                finally:
                    self._running = False
                    if claim is not None and 'replay' not in claim:
                        try:
                            self.state.release(claim)
                        except Exception:
                            logger.exception('Legacy import claim release failed; lease will expire')

    def _legacy_record_error(self, claim):
        if claim is None or 'replay' in claim:
            return
        error = sys.exc_info()[1]
        try:
            def failed(saved):
                saved['status'] = 'error'
                saved['last_error_code'] = str(error) if isinstance(error, BucketIdempotencyError) else 'import_failed'
                if len(saved['errors']) < 100:
                    saved['errors'].append('import_failed')
            self.state.checkpoint(claim, change=failed)
        except Exception:
            logger.exception('Legacy import failure checkpoint unavailable')

    def _legacy_child_key(self, claim, chunk, item, effect):
        return 's4c:' + self._legacy_digest(
            ['legacy-item-v1', self.state.root, claim['run_id'], chunk, item, effect])

    async def _legacy_process_chunk(self, claim, ordinal, chunk):
        saved = self.state.fence(claim)
        if saved['chunks'][ordinal]['digest'] != self._legacy_digest(chunk):
            raise BucketIdempotencyError('legacy_chunk_conflict')
        if saved['chunks'][ordinal]['items'] is None:
            self.state.checkpoint(claim, 'chunk.extraction_pending')
            try:
                items = await self._extract_memories(chunk['content']) if chunk['content'].strip() else []
                outcome = 'extracted'
            except (BucketIdempotencyError, MaintenanceWriteError, OSError):
                raise
            except Exception:
                logger.exception('Legacy extraction failed index=%d', ordinal)
                items, outcome = [], 'provider_failed'
            def freeze(state):
                current = state['chunks'][ordinal]
                current.update(status='extraction_frozen', outcome=outcome, api_calls=1,
                    items=[dict(ordinal=i, digest=self._legacy_digest(item), extraction=copy.deepcopy(item),
                                status='extraction_frozen', plan=None, resolutions={})
                           for i, item in enumerate(items)])
            self.state.checkpoint(claim, 'chunk.extraction_frozen', freeze)
        for item_ordinal in range(len(self.state.fence(claim)['chunks'][ordinal]['items'])):
            context = dict(state=self.state, claim=claim, chunk=ordinal, item=item_ordinal)
            current = self.state.fence(claim)['chunks'][ordinal]['items'][item_ordinal]
            if current['status'] == 'completed':
                continue
            if current['ordinal'] != item_ordinal or current['digest'] != self._legacy_digest(current['extraction']):
                raise BucketIdempotencyError('legacy_item_conflict')
            if current['plan'] is None:
                item = current['extraction']
                preserve = self.state.fence(claim)['preserve_raw'] or item.get('preserve_raw', False)
                candidate = None
                if not preserve:
                    try:
                        existing = await self.bucket_mgr.search(item['content'], limit=1,
                                                               domain_filter=item.get('domain', ['未分类']) or None)
                    except (BucketIdempotencyError, MaintenanceWriteError, OSError):
                        raise
                    except Exception:
                        existing = []
                    if existing:
                        meta = existing[0].get('metadata', {})
                        if (not meta.get('sealed') and meta.get('type') != 'feel'
                                and not (meta.get('pinned') or meta.get('protected'))
                                and self.bucket_mgr._normalize_search_text(existing[0].get('content', ''))
                                == self.bucket_mgr._normalize_search_text(item['content'])):
                            candidate = existing[0]['id']
                self._legacy_plan_item(context, item, candidate, preserve)
            await self._legacy_execute_item(context)
        def complete(state):
            current = state['chunks'][ordinal]
            if not all(i['status'] == 'completed' for i in current['items']):
                raise BucketIdempotencyError('legacy_item_incomplete')
            current['status'] = 'completed'
        self.state.checkpoint(claim, 'chunk.completed', complete)

    @guarded_mutation('legacy_import_item_plan')
    def _legacy_plan_item(self, context, item, candidate, preserve):
        with bucket_write_scope(self.state.root):
            saved = self.state.fence(context['claim'])
            current = saved['chunks'][context['chunk']]['items'][context['item']]
            if current['plan'] is not None:
                return
            memory_key = self._legacy_child_key(context['claim'], context['chunk'], context['item'], 'memory')
            plan = self.bucket_mgr.plan_legacy_import_item(item, candidate, preserve,
                memory_key=memory_key, embedding_key=self._legacy_child_key(
                    context['claim'], context['chunk'], context['item'], 'embedding'),
                aliases=saved['pipeline']['aliases'])
            def freeze(state):
                entry = state['chunks'][context['chunk']]['items'][context['item']]
                entry.update(plan=plan, status='planned')
            self.state.checkpoint(context['claim'], 'item.planned', freeze)

    def _legacy_item_checkpoint(self, context, phase, resolution=None):
        def change(saved):
            item = saved['chunks'][context['chunk']]['items'][context['item']]
            item['status'] = phase.removeprefix('item.')
            if resolution:
                item['resolutions'].update(resolution)
        return self.state.checkpoint(context['claim'], phase, change)

    async def _legacy_execute_item(self, context):
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        plan = current['plan']
        if 'memory' not in current['resolutions']:
            if plan['decision'] == 'reuse' and not plan['memory_requested']:
                self.bucket_mgr.validate_legacy_import_target(context)
                outcome = 'not_requested'
            else:
                # A draft exists only until the existing child journal owns the payload.
                self.bucket_mgr.ensure_legacy_import_operation(context)
                def journal_owned(saved):
                    entry = saved['chunks'][context['chunk']]['items'][context['item']]
                    entry['plan'].pop('payload', None)
                self.state.checkpoint(context['claim'], change=journal_owned)
                await self.bucket_mgr.apply_import_operation(plan['memory_key'], _legacy_import_context=context)
                outcome = 'applied'
            self._legacy_item_checkpoint(context, 'item.memory_applied', {'memory': outcome})
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        if 'delta' not in current['resolutions']:
            receipt = self.bucket_mgr.commit_legacy_import_delta(context)
            self._legacy_item_checkpoint(context, 'item.memory_applied', {'delta': receipt['outcome']})
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        if 'embedding' not in current['resolutions']:
            receipt = await self._legacy_embedding(context)
            self._legacy_item_checkpoint(context, 'item.memory_applied', {'embedding': receipt['outcome']})
        self._legacy_item_checkpoint(context, 'item.effects_resolved')
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        if not all(step in current['resolutions'] for step in ('memory', 'delta', 'embedding')):
            raise BucketIdempotencyError('legacy_effect_incomplete')
        self._legacy_item_checkpoint(context, 'item.completed')

    async def _legacy_embedding(self, context):
        plan = self.bucket_mgr._legacy_import_plan(context)
        if plan['decision'] == 'reuse':
            return {'outcome': 'not_requested'}
        operation = self.bucket_mgr.inspect_import_operation(plan['memory_key'])
        embedding_input = operation['payload']['content']
        if hashlib.sha256(embedding_input.encode()).hexdigest() != plan['body_digest']:
            raise BucketIdempotencyError('operation_payload_conflict')
        if not embedding_input.strip():
            return {'outcome': 'not_requested'}
        engine = self.embedding_engine or self.bucket_mgr.embedding_engine
        if not engine or not engine.enabled:
            return {'outcome': 'disabled'}
        receipt = engine.trace_embedding_receipt(plan['embedding_key'])
        if receipt is not None:
            return receipt
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        candidate = current['resolutions'].get('embedding_candidate')
        if candidate is None:
            with bucket_write_scope(self.bucket_mgr.base_dir):
                guard = current['resolutions'].get('source_guard') or self.bucket_mgr.delete_admission.capture(plan['target'])
                self.bucket_mgr.admit_delayed_effect(plan['target'],guard,'legacy_import_embedding')
                self._legacy_item_checkpoint(context,'item.memory_applied',{'source_guard':guard})
            try:
                candidate = await engine._generate_embedding(embedding_input, model=plan['embedding_model'])
            except (BucketIdempotencyError, MaintenanceWriteError, OSError):
                raise
            except Exception:
                candidate = []
            self._legacy_item_checkpoint(context, 'item.memory_applied', {'embedding_candidate': candidate})
        if not candidate:
            return {'outcome': 'failed'}
        current = self.state.fence(context['claim'])['chunks'][context['chunk']]['items'][context['item']]
        context = dict(context,source_guard=current['resolutions'].get('source_guard'))
        return self.bucket_mgr.commit_legacy_import_embedding(context, engine, candidate)

    async def start_raw_evidence(
        self,
        raw_bytes: bytes,
        filename: str = "",
        preserve_raw: bool = False,
        resume: bool = False,
        media_type: str = "application/octet-stream",
    ) -> dict:
        """Run the opt-in O5B path; the legacy ``start`` path is untouched."""

        if self._running:
            return {"error": "Import already running"}

        from raw_evidence_import import RawEvidenceImportCoordinator
        from raw_evidence_store import RawEvidenceError

        self._running = True
        self._paused = False
        coordinator = None
        prepared = None
        try:
            coordinator = RawEvidenceImportCoordinator(self.config)
            prepared = coordinator.prepare_run(
                raw_bytes,
                filename=filename,
                media_type=media_type,
                preserve_raw=preserve_raw,
                resume=resume,
            )
            coordinator.capture(
                prepared,
                raw_bytes,
                filename=filename,
                media_type=media_type,
            )
            coordinator.reconcile_pending_lineage(
                self.bucket_mgr,
                run_id=prepared.run_id,
            )

            # Capture is verified before this lossy transformation is allowed.
            raw_content = raw_bytes.decode("utf-8", errors="replace")
            turns = detect_and_parse(raw_content, filename)
            if not turns:
                coordinator.update_run(
                    prepared.run_id,
                    status="failed",
                    error_category="parse_no_turns",
                )
                return {"error": "No conversation turns found in file"}

            self._chunks = chunk_turns(turns)
            if not self._chunks:
                coordinator.update_run(
                    prepared.run_id,
                    status="failed",
                    error_category="parse_no_chunks",
                )
                return {"error": "No processable chunks after splitting"}

            source_hash_prefix = prepared.source_sha256[:16]
            can_reuse_legacy_state = (
                resume
                and self.state.load()
                and self.state.data.get("source_hash") == source_hash_prefix
                and self.state.data.get("status") in {"paused", "running", "error"}
            )
            if not can_reuse_legacy_state:
                self.state.reset(filename, source_hash_prefix, len(self._chunks))
            else:
                self.state.data["source_file"] = filename
                self.state.data["total_chunks"] = len(self._chunks)
                self.state.data["status"] = "running"
            self.state.data["raw_evidence_capture"] = True
            self.state.data["processed"] = min(
                int(prepared.run.get("processed_chunks", 0) or 0),
                len(self._chunks),
            )
            self.state.save()
            coordinator.update_run(
                prepared.run_id,
                status="processing",
                total_chunks=len(self._chunks),
                processed_chunks=self.state.data["processed"],
            )
            return await self._process_raw_evidence_chunks(
                coordinator,
                prepared.run_id,
                preserve_raw,
            )
        except asyncio.CancelledError:
            # Only this admitted worker owns cleanup; HTTP callers do not.
            try:
                if coordinator is not None and prepared is not None:
                    run = coordinator.store.get_import_run(prepared.run_id)
                    saved, _ = self.state.read_legacy()
                    if run["status"] != "completed" and (not saved or saved.get("status") != "completed"):
                        try:
                            coordinator.update_run(prepared.run_id, status="paused")
                        except Exception:
                            logger.exception("O5B cancellation run pause save failed")
                        previous_status = self.state.data["status"]
                        self.state.data["status"] = "paused"
                        try:
                            self.state.save()
                        except Exception:
                            self.state.data["status"] = previous_status
                            logger.exception("O5B cancellation progress pause save failed")
            except Exception:
                logger.exception("O5B cancellation state inspection failed")
            raise
        except RawEvidenceError as exc:
            if coordinator is not None and prepared is not None:
                try:
                    coordinator.update_run(
                        prepared.run_id,
                        status="failed",
                        error_category=exc.code,
                    )
                except Exception:
                    logger.exception("O5B run failure state update failed")
            self.state.data["status"] = "error"
            self.state.data["errors"].append("import_failed")
            try:
                self.state.save()
            except Exception:
                logger.exception("O5B failure progress save failed")
                raise
            raise
        except Exception:
            if coordinator is not None and prepared is not None:
                try:
                    coordinator.update_run(
                        prepared.run_id,
                        status="failed",
                        error_category="import_failed",
                    )
                except Exception:
                    logger.exception("O5B run failure state update failed")
            self.state.data["status"] = "error"
            self.state.data["errors"].append("import_failed")
            try:
                self.state.save()
            except Exception:
                logger.exception("O5B failure progress save failed")
                raise
            logger.exception("O5B import failed")
            raise
        finally:
            self._running = False

    @staticmethod
    def _o5b_digest(value: Any) -> str:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _o5b_operation_key(run_id: str, chunk_index: int, item_index: int) -> str:
        material = f"{run_id}:{chunk_index}:{item_index}".encode("utf-8")
        return "o5b:" + hashlib.sha256(material).hexdigest()

    @staticmethod
    def _o5c_memory_mutation_id(operation_key: str) -> str:
        return hashlib.sha256(
            f"ombre-brain:o5c:memory-mutation:{operation_key}".encode("utf-8")
        ).hexdigest()

    @staticmethod
    def _o5c_lineage_kind(item_kind: str, operation_kind: str) -> str:
        if item_kind == "memory_raw":
            return "preserve_raw_created"
        if operation_kind == "update":
            return "contributed_update"
        return "created"

    @staticmethod
    def _o5b_create_payload(item: dict) -> dict:
        todos = canonicalize_todos(item.get("todos"))
        payload = {
            "content": item["content"],
            "tags": item.get("tags", []),
            "importance": item.get("importance", 5),
            "domain": item.get("domain", ["未分类"]),
            "valence": item.get("valence", 0.5),
            "arousal": item.get("arousal", 0.3),
            "name": item.get("name") or None,
            "todos": todos,
        }
        if todos:
            payload["todo_provenance"] = automatic_todo_provenance(todos)
        return payload

    async def _o5b_build_operation(self, item: dict, preserve_raw: bool) -> tuple[str, str | None, dict, bool]:
        """Select and fully plan the memory side effect before applying it."""

        if preserve_raw or item.get("preserve_raw", False):
            return "create", None, self._o5b_create_payload(item), False

        content = item["content"]
        domain = item.get("domain", ["未分类"])
        tags = item.get("tags", [])
        importance = item.get("importance", 5)
        valence = item.get("valence", 0.5)
        arousal = item.get("arousal", 0.3)
        name = item.get("name", "")
        incoming_todos = canonicalize_todos(apply_display_aliases_to_value(item.get("todos")))

        try:
            existing = await self.bucket_mgr.search(
                content,
                limit=1,
                domain_filter=domain or None,
            )
        except Exception:
            existing = []

        merge_threshold = self.config.get("merge_threshold", 75)
        if existing and existing[0].get("score", 0) > merge_threshold:
            bucket = existing[0]
            # O5B keeps its durable planned-update and lineage semantics.
            if (
                bucket["metadata"].get("type") != "feel"
                and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected"))
            ):
                try:
                    merged = await self.dehydrator.merge(bucket["content"], content)
                    self.state.data["api_calls"] += 1
                    old_v = bucket["metadata"].get("valence", 0.5)
                    old_a = bucket["metadata"].get("arousal", 0.3)
                    existing_todos = canonicalize_todos(
                        bucket["metadata"].get("todos")
                    )
                    merged_todos, merged_provenance = merge_todo_provenance(
                        existing_todos, bucket["metadata"].get("todo_provenance"),
                        incoming_todos, automatic_todo_provenance(incoming_todos),
                    )
                    return (
                        "update",
                        bucket["id"],
                        {
                            "kwargs": {
                                "content": merged,
                                "tags": list(set(bucket["metadata"].get("tags", []) + tags)),
                                "importance": max(bucket["metadata"].get("importance", 5), importance),
                                "domain": list(set(bucket["metadata"].get("domain", []) + domain)),
                                "valence": round((old_v + valence) / 2, 2),
                                "arousal": round((old_a + arousal) / 2, 2),
                                "todos": merged_todos,
                                **({"todo_provenance": merged_provenance} if incoming_todos else {}),
                            }
                        },
                        True,
                    )
                except Exception:
                    logger.warning("O5B merge planning failed; using create")
                    self.state.data["api_calls"] += 1

        return "create", None, self._o5b_create_payload(item), False

    async def _o5b_plan_item(
        self,
        coordinator,
        run_id: str,
        chunk_index: int,
        item_index: int,
        item: dict,
        preserve_raw: bool,
    ) -> dict:
        operation_key = self._o5b_operation_key(run_id, chunk_index, item_index)
        input_digest = self._o5b_digest(item)
        persisted = self.bucket_mgr.inspect_import_operation(operation_key)
        if persisted is not None:
            # The memory plan may have committed before the item checkpoint.
            # Its identities and body are authoritative; do not plan a second merge.
            operation_kind = persisted["operation_kind"]
            target_bucket_id = persisted["target_bucket_id"]
            payload = persisted["payload"]
        else:
            operation_kind, target_bucket_id, payload, _merged = await self._o5b_build_operation(
                item,
                preserve_raw,
            )
        snapshot = coordinator.get_item(run_id, "source_snapshot")
        if not snapshot or not snapshot.get("evidence_id") or not snapshot.get("revision_id"):
            raise RuntimeError("source_snapshot_lineage_missing")
        item_key = f"chunk:{chunk_index}:item:{item_index}"
        item_kind = (
            "memory_raw"
            if preserve_raw or item.get("preserve_raw", False)
            else "memory"
        )
        memory_mutation_id = self._o5c_memory_mutation_id(operation_key)
        operation = self.bucket_mgr.plan_import_operation(
            operation_key,
            operation_kind=operation_kind,
            target_bucket_id=target_bucket_id,
            payload=payload,
            memory_mutation_id=memory_mutation_id,
        )
        record = coordinator.upsert_item(
            run_id,
            item_key,
            item_kind=item_kind,
            input_digest=input_digest,
            status="memory_planned",
            evidence_id=snapshot["evidence_id"],
            revision_id=snapshot["revision_id"],
            operation_key=operation_key,
            operation_kind=operation_kind,
            target_bucket_id=target_bucket_id,
            payload_digest=operation["payload_digest"],
            result_id=operation["result_id"],
        )
        lineage = coordinator.create_lineage_intent(
            run_id=run_id,
            run_item_key=item_key,
            operation_key=operation_key,
            memory_id=operation["result_id"],
            memory_mutation_id=memory_mutation_id,
            evidence_id=snapshot["evidence_id"],
            revision_id=snapshot["revision_id"],
            lineage_kind=self._o5c_lineage_kind(item_kind, operation_kind),
        )
        record["lineage_id"] = lineage["lineage_id"]
        record["memory_mutation_id"] = memory_mutation_id
        return record

    async def _o5c_ensure_lineage(self, coordinator, run_id: str, record: dict) -> dict:
        """Ensure a capture item has one durable lineage intent."""

        operation_key = record.get("operation_key")
        evidence_id = record.get("evidence_id")
        revision_id = record.get("revision_id")
        memory_id = record.get("result_id") or record.get("target_bucket_id")
        if not operation_key or not evidence_id or not revision_id or not memory_id:
            raise RuntimeError("lineage_identity_missing")
        mutation_id = self._o5c_memory_mutation_id(operation_key)
        lineage = coordinator.create_lineage_intent(
            run_id=run_id,
            run_item_key=record["item_key"],
            operation_key=operation_key,
            memory_id=memory_id,
            memory_mutation_id=mutation_id,
            evidence_id=evidence_id,
            revision_id=revision_id,
            lineage_kind=self._o5c_lineage_kind(
                record["item_kind"], record["operation_kind"]
            ),
        )
        record["lineage_id"] = lineage["lineage_id"]
        record["memory_mutation_id"] = mutation_id
        return lineage

    async def _o5b_apply_item(self, coordinator, run_id: str, record: dict) -> tuple[str, str]:
        if record["status"] == "succeeded":
            lineage = await self._o5c_ensure_lineage(coordinator, run_id, record)
            if lineage["status"] == "pending":
                coordinator.reconcile_pending_lineage(
                    self.bucket_mgr,
                    run_id=run_id,
                )
                lineage = coordinator.store.get_lineage(lineage["lineage_id"])
            if not lineage or lineage["status"] != "complete":
                raise RuntimeError("lineage_pending")
            return record["operation_kind"], record.get("result_id") or ""
        operation_key = record.get("operation_key")
        if not operation_key:
            raise RuntimeError("operation_plan_missing")
        lineage = await self._o5c_ensure_lineage(coordinator, run_id, record)
        if lineage["status"] not in {"pending", "complete"}:
            raise RuntimeError("lineage_provenance_broken")
        if lineage["status"] == "complete":
            result_id = record.get("result_id") or record.get("target_bucket_id") or ""
            coordinator.upsert_item(
                run_id,
                record["item_key"],
                item_kind=record["item_kind"],
                input_digest=record["input_digest"],
                status="succeeded",
                evidence_id=record.get("evidence_id"),
                revision_id=record.get("revision_id"),
                operation_key=record["operation_key"],
                operation_kind=record["operation_kind"],
                target_bucket_id=record.get("target_bucket_id"),
                payload_digest=record.get("payload_digest"),
                result_id=result_id,
            )
            return record["operation_kind"], result_id
        coordinator.upsert_item(
            run_id,
            record["item_key"],
            item_kind=record["item_kind"],
            input_digest=record["input_digest"],
            status="memory_applying",
            operation_key=record["operation_key"],
            operation_kind=record["operation_kind"],
            target_bucket_id=record.get("target_bucket_id"),
            payload_digest=record.get("payload_digest"),
            result_id=record.get("result_id"),
        )
        try:
            result = await self.bucket_mgr.apply_import_operation(
                operation_key,
                memory_mutation_id=record["memory_mutation_id"],
            )
        except Exception:
            coordinator.upsert_item(
                run_id,
                record["item_key"],
                item_kind=record["item_kind"],
                input_digest=record["input_digest"],
                status="failed",
                operation_key=record["operation_key"],
                operation_kind=record["operation_kind"],
                target_bucket_id=record.get("target_bucket_id"),
                payload_digest=record.get("payload_digest"),
                result_id=record.get("result_id"),
                error_category="memory_apply_failed",
            )
            raise
        result_id = result.get("result_id") or record.get("result_id") or ""
        if result_id != record.get("result_id"):
            coordinator.store.update_lineage_status(
                lineage["lineage_id"], status="provenance_broken"
            )
            raise RuntimeError("lineage_result_conflict")
        coordinator.complete_lineage(lineage["lineage_id"])
        coordinator.upsert_item(
            run_id,
            record["item_key"],
            item_kind=record["item_kind"],
            input_digest=record["input_digest"],
            status="succeeded",
            operation_key=record["operation_key"],
            operation_kind=record["operation_kind"],
            target_bucket_id=record.get("target_bucket_id"),
            payload_digest=record.get("payload_digest"),
            result_id=result_id,
        )
        return record["operation_kind"], result_id

    async def _process_raw_evidence_chunks(self, coordinator, run_id: str, preserve_raw: bool) -> dict:
        start_idx = int(self.state.data.get("processed", 0) or 0)
        for i in range(start_idx, len(self._chunks)):
            if self._paused:
                self.state.data["status"] = "paused"
                self.state.save()
                coordinator.update_run(run_id, status="paused", processed_chunks=i)
                return self.state.to_dict()

            chunk = self._chunks[i]
            chunk_key = f"chunk:{i}"
            chunk_digest = hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest()
            chunk_record = coordinator.get_item(run_id, chunk_key)
            try:
                if chunk_record and chunk_record["status"] in {"extraction_ready", "succeeded"}:
                    item_records = coordinator.list_items(
                        run_id,
                        prefix=f"chunk:{i}:item:",
                    )
                else:
                    coordinator.upsert_item(
                        run_id,
                        chunk_key,
                        item_kind="chunk",
                        input_digest=chunk_digest,
                        status="extraction_pending",
                    )
                    try:
                        items = await self._extract_memories(chunk["content"])
                        self.state.data["api_calls"] += 1
                    except Exception:
                        coordinator.upsert_item(
                            run_id,
                            chunk_key,
                            item_kind="chunk",
                            input_digest=chunk_digest,
                            status="extraction_failed",
                            error_category="extraction_failed",
                        )
                        raise

                    item_records = []
                    for item_index, item in enumerate(items):
                        item_records.append(
                            await self._o5b_plan_item(
                                coordinator,
                                run_id,
                                i,
                                item_index,
                                item,
                                preserve_raw,
                            )
                        )
                    coordinator.upsert_item(
                        run_id,
                        chunk_key,
                        item_kind="chunk",
                        input_digest=chunk_digest,
                        status="extraction_ready",
                        item_count=len(item_records),
                    )

                for record in item_records:
                    operation_kind, _result_id = await self._o5b_apply_item(
                        coordinator,
                        run_id,
                        record,
                    )
                    if record["status"] != "succeeded":
                        if record["item_kind"] == "memory_raw":
                            self.state.data["memories_raw"] += 1
                            self.state.data["memories_created"] += 1
                        elif operation_kind == "update":
                            self.state.data["memories_merged"] += 1
                        else:
                            self.state.data["memories_created"] += 1

                coordinator.upsert_item(
                    run_id,
                    chunk_key,
                    item_kind="chunk",
                    input_digest=chunk_digest,
                    status="succeeded",
                    item_count=len(item_records),
                )
                self.state.data["processed"] = i + 1
                self.state.save()
                coordinator.update_run(
                    run_id,
                    status="processing",
                    processed_chunks=i + 1,
                )
            except Exception:
                if len(self.state.data["errors"]) < 100:
                    self.state.data["errors"].append("import_failed")
                self.state.data["status"] = "error"
                self.state.save()
                try:
                    coordinator.update_run(
                        run_id,
                        status="failed",
                        error_category="import_failed",
                    )
                except Exception:
                    logger.exception("O5B run failure state update failed")
                logger.exception("O5B import chunk failed index=%d", i)
                return self.state.to_dict()

        self.state.data["status"] = "completed"
        self.state.save()
        coordinator.update_run(
            run_id,
            status="completed",
            processed_chunks=len(self._chunks),
        )
        return self.state.to_dict()

    async def _process_chunks(self, preserve_raw: bool) -> dict:
        """Process chunks from current position."""
        start_idx = self.state.data["processed"]

        for i in range(start_idx, len(self._chunks)):
            if self._paused:
                self.state.data["status"] = "paused"
                self.state.save()
                self._running = False
                logger.info(f"Import paused at chunk {i}/{len(self._chunks)}")
                return self.state.to_dict()

            chunk = self._chunks[i]
            try:
                await self._process_single_chunk(chunk, preserve_raw)
            except Exception:
                err_msg = "import_failed"; logger.exception("Import chunk failed index=%d", i)
                if len(self.state.data["errors"]) < 100: self.state.data["errors"].append(err_msg)

            self.state.data["processed"] = i + 1
            # Save progress every chunk
            self.state.save()

        self.state.data["status"] = "completed"
        self.state.save()
        self._running = False
        logger.info(f"Import completed: {self.state.data['memories_created']} created, {self.state.data['memories_merged']} merged")
        return self.state.to_dict()

    async def _process_single_chunk(self, chunk: dict, preserve_raw: bool):
        """Extract memories from a single chunk and store them."""
        content = chunk["content"]
        if not content.strip():
            return

        # --- LLM extraction ---
        try:
            items = await self._extract_memories(content)
            self.state.data["api_calls"] += 1
        except Exception as e:
            logger.warning(f"LLM extraction failed: {e}")
            self.state.data["api_calls"] += 1
            return

        if not items:
            return

        # --- Store each extracted memory ---
        for item in items:
            try:
                should_preserve = preserve_raw or item.get("preserve_raw", False)

                if should_preserve:
                    # Raw mode: store original content without summarization
                    bucket_id = await self.bucket_mgr.create(
                        content=item["content"],
                        tags=item.get("tags", []),
                        importance=item.get("importance", 5),
                        domain=item.get("domain", ["未分类"]),
                        valence=item.get("valence", 0.5),
                        arousal=item.get("arousal", 0.3),
                        name=item.get("name"),
                        todos=canonicalize_todos(item.get("todos")),
                        todo_provenance=automatic_todo_provenance(item.get("todos")),
                    )
                    self.state.data["memories_raw"] += 1
                    self.state.data["memories_created"] += 1
                else:
                    # Normal mode: go through merge-or-create pipeline
                    is_merged = await self._merge_or_create_item(item)
                    if is_merged:
                        self.state.data["memories_merged"] += 1
                    else:
                        self.state.data["memories_created"] += 1

                # Patch timestamp if available
                if chunk.get("timestamp_start"):
                    # We don't have update support for created, so skip
                    pass

            except Exception as e:
                logger.warning(f"Failed to store memory: {item.get('name', '?')}: {e}")

    async def _extract_memories(self, chunk_content: str) -> list[dict]:
        """Use LLM to extract memories from a conversation chunk."""
        if not self.dehydrator.api_available:
            raise RuntimeError("API not available")

        response = await self.dehydrator.client.chat.completions.create(
            model=self.dehydrator.model,
            messages=[
                {"role": "system", "content": IMPORT_EXTRACT_PROMPT},
                {"role": "user", "content": chunk_content[:12000]},
            ],
            max_tokens=2048,
            temperature=0.0,
        )

        if not response.choices:
            return []

        raw = response.choices[0].message.content or ""
        if not raw.strip():
            return []

        return self._parse_extraction(raw)

    @staticmethod
    def _parse_extraction(raw: str) -> list[dict]:
        """Parse and validate LLM extraction result."""
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
            items = json.loads(cleaned)
        except (json.JSONDecodeError, IndexError, ValueError):
            logger.warning(f"Import extraction JSON parse failed: {raw[:200]}")
            return []

        if not isinstance(items, list):
            return []

        validated = []
        for item in items:
            if not isinstance(item, dict) or not item.get("content"):
                continue
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (ValueError, TypeError):
                importance = 5
            try:
                valence = max(0.0, min(1.0, float(item.get("valence", 0.5))))
                arousal = max(0.0, min(1.0, float(item.get("arousal", 0.3))))
            except (ValueError, TypeError):
                valence, arousal = 0.5, 0.3
            raw_todos = item.get("todos", [])
            todos = (
                list(dict.fromkeys(
                    str(todo).strip()
                    for todo in raw_todos
                    if todo is not None and str(todo).strip()
                ))[:20]
                if isinstance(raw_todos, list)
                else []
            )

            validated.append({
                "name": str(item.get("name", ""))[:20],
                "content": str(item["content"]),
                "domain": item.get("domain", ["未分类"])[:3],
                "valence": valence,
                "arousal": arousal,
                "tags": [str(t) for t in item.get("tags", [])][:10],
                "importance": importance,
                "todos": todos,
                "preserve_raw": bool(item.get("preserve_raw", False)),
                "is_pattern": bool(item.get("is_pattern", False)),
            })

        return validated

    async def _merge_or_create_item(self, item: dict) -> bool:
        """Reuse only deterministic duplicates; otherwise create a new bucket."""
        content = item["content"]
        domain = item.get("domain", ["未分类"])
        tags = item.get("tags", [])
        importance = item.get("importance", 5)
        valence = item.get("valence", 0.5)
        arousal = item.get("arousal", 0.3)
        name = item.get("name", "")

        try:
            existing = await self.bucket_mgr.search(content, limit=1, domain_filter=domain or None)
        except Exception:
            existing = []

        if existing:
            bucket = existing[0]
            metadata = bucket.get("metadata", {})
            if (
                not metadata.get("sealed")
                and metadata.get("type") != "feel"
                and self.bucket_mgr._normalize_search_text(bucket.get("content", ""))
                == self.bucket_mgr._normalize_search_text(content)
                and not (metadata.get("pinned") or metadata.get("protected"))
            ):
                # Generic imports may reuse only deterministic textual duplicates.
                incoming_todos = canonicalize_todos(apply_display_aliases_to_value(item.get("todos")))
                if incoming_todos:
                    existing_todos = canonicalize_todos(metadata.get("todos"))
                    existing_provenance = reconcile_todo_provenance(
                        existing_todos, metadata.get("todo_provenance")
                    )
                    merged_todos, merged_provenance = merge_todo_provenance(
                        existing_todos, existing_provenance,
                        incoming_todos, automatic_todo_provenance(incoming_todos),
                    )
                    if (
                        merged_todos != existing_todos
                        or merged_provenance != existing_provenance
                        or not isinstance(metadata.get("todos"), list)
                    ):
                        updated = await self.bucket_mgr.update(
                            bucket["id"],
                            todos=merged_todos,
                            todo_provenance=merged_provenance,
                        )
                        if not updated:
                            raise RuntimeError(
                                f"failed to preserve todos for duplicate bucket {bucket['id']}"
                            )
                return True

        # Create new
        bucket_id = await self.bucket_mgr.create(
            content=content,
            tags=tags,
            importance=importance,
            domain=domain,
            valence=valence,
            arousal=arousal,
            name=name or None,
            todos=canonicalize_todos(item.get("todos")),
            todo_provenance=automatic_todo_provenance(item.get("todos")),
        )
        return False

    async def detect_patterns(self) -> list[dict]:
        """
        Post-import: detect high-frequency patterns via embedding clustering.
        导入后：通过 embedding 聚类检测高频模式。
        Returns list of {pattern_content, count, bucket_ids, suggested_action}.
        """
        if not self.embedding_engine:
            return []

        all_buckets = await self.bucket_mgr.list_all(include_archive=False)
        dynamic_buckets = [
            b for b in all_buckets
            if b["metadata"].get("type") == "dynamic"
            and not b["metadata"].get("pinned")
            and not b["metadata"].get("resolved")
        ]

        if len(dynamic_buckets) < 5:
            return []

        # Get embeddings
        embeddings = {}
        for b in dynamic_buckets:
            emb = await self.embedding_engine.get_embedding(b["id"])
            if emb is not None:
                embeddings[b["id"]] = emb

        if len(embeddings) < 5:
            return []

        # Find clusters: group by pairwise similarity > 0.7
        import numpy as np
        ids = list(embeddings.keys())
        clusters: dict[str, list[str]] = {}
        visited = set()

        for i, id_a in enumerate(ids):
            if id_a in visited:
                continue
            cluster = [id_a]
            visited.add(id_a)
            emb_a = np.array(embeddings[id_a])
            norm_a = np.linalg.norm(emb_a)
            if norm_a == 0:
                continue

            for j in range(i + 1, len(ids)):
                id_b = ids[j]
                if id_b in visited:
                    continue
                emb_b = np.array(embeddings[id_b])
                norm_b = np.linalg.norm(emb_b)
                if norm_b == 0:
                    continue
                sim = float(np.dot(emb_a, emb_b) / (norm_a * norm_b))
                if sim > 0.7:
                    cluster.append(id_b)
                    visited.add(id_b)

            if len(cluster) >= 3:
                clusters[id_a] = cluster

        # Format results
        patterns = []
        for lead_id, cluster_ids in clusters.items():
            lead_bucket = next((b for b in dynamic_buckets if b["id"] == lead_id), None)
            if not lead_bucket:
                continue
            patterns.append({
                "pattern_content": lead_bucket["content"][:200],
                "pattern_name": lead_bucket["metadata"].get("name", lead_id),
                "count": len(cluster_ids),
                "bucket_ids": cluster_ids,
                "suggested_action": "pin" if len(cluster_ids) >= 5 else "review",
            })

        patterns.sort(key=lambda p: p["count"], reverse=True)
        return patterns[:20]
