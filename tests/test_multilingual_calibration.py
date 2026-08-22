"""Bounded offline calibration for ordinary-memory multilingual retrieval."""

import pytest

from bucket_manager import BucketManager


class DeterministicSemanticSearch:
    """Small semantic stub with explicit, repeatable scores per query."""

    enabled = True
    last_error = ""

    def __init__(self, scores):
        self.scores = scores
        self.calls = []

    async def search_similar(self, query, top_k, candidate_ids=None):
        allowed = set(candidate_ids or ())
        self.calls.append((query, top_k, allowed))
        return [
            (bucket_id, score)
            for bucket_id, score in self.scores[query].items()
            if not allowed or bucket_id in allowed
        ]


def _bucket(bucket_id, *, name, keywords, content):
    return {
        "id": bucket_id,
        "content": content,
        "metadata": {
            "name": name,
            "domain": ["ordinary-memory"],
            "keywords": keywords,
            "tags": [],
            "type": "dynamic",
            "importance": 5,
            "valence": 0.5,
            "arousal": 0.3,
        },
    }


@pytest.fixture
def multilingual_fixture():
    buckets = [
        _bucket(
            "en-relevant",
            name="Incident response",
            keywords=["incident response"],
            content="Incident response runbook for service outages.",
        ),
        _bucket(
            "en-unrelated",
            name="Weekend gardening",
            keywords=["gardening"],
            content="Notes about planting herbs.",
        ),
        _bucket(
            "zh-relevant",
            name="旅行计划",
            keywords=["旅行计划"],
            content="春季旅行计划与行程安排。",
        ),
        _bucket(
            "zh-unrelated",
            name="账单记录",
            keywords=["账单"],
            content="本月家庭账单记录。",
        ),
        _bucket(
            "zh-query-en-memory",
            name="Database migration runbook",
            keywords=["database migration"],
            content="Database migration runbook with rollback steps.",
        ),
        _bucket(
            "zh-query-en-unrelated",
            name="Quarterly hiring notes",
            keywords=["hiring"],
            content="Quarterly hiring notes and interview scheduling.",
        ),
        _bucket(
            "en-query-zh-memory",
            name="发布检查清单",
            keywords=["发布检查清单"],
            content="发布检查清单：验证版本、回滚和监控。",
        ),
        _bucket(
            "en-query-zh-unrelated",
            name="午餐菜单",
            keywords=["午餐"],
            content="本周午餐菜单和采购清单。",
        ),
        _bucket(
            "mixed-relevant",
            name="Python 性能优化",
            keywords=["Python 性能优化"],
            content="Python 性能优化笔记：profiling and caching.",
        ),
        _bucket(
            "mixed-unrelated",
            name="JavaScript 旅行照片",
            keywords=["JavaScript", "旅行"],
            content="JavaScript demo and travel photo notes.",
        ),
    ]
    cases = [
        {
            "label": "English",
            "query": "incident response",
            "relevant": "en-relevant",
            "unrelated": "en-unrelated",
            "semantic_score": 0.78,
        },
        {
            "label": "Chinese",
            "query": "旅行计划",
            "relevant": "zh-relevant",
            "unrelated": "zh-unrelated",
            "semantic_score": 0.81,
        },
        {
            "label": "Chinese query to English memory",
            "query": "数据库迁移",
            "relevant": "zh-query-en-memory",
            "unrelated": "zh-query-en-unrelated",
            "semantic_score": 0.93,
        },
        {
            "label": "English query to Chinese memory",
            "query": "release checklist",
            "relevant": "en-query-zh-memory",
            "unrelated": "en-query-zh-unrelated",
            "semantic_score": 0.91,
        },
        {
            "label": "Mixed language",
            "query": "Python 性能优化",
            "relevant": "mixed-relevant",
            "unrelated": "mixed-unrelated",
            "semantic_score": 0.86,
        },
    ]
    return buckets, cases


@pytest.mark.asyncio
async def test_multilingual_retrieval_calibration_passes_existing_gate(
    test_config, multilingual_fixture
):
    buckets, cases = multilingual_fixture
    scores = {
        case["query"]: {
            case["relevant"]: case["semantic_score"],
            case["unrelated"]: 0.10,
        }
        for case in cases
    }
    semantic = DeterministicSemanticSearch(scores)
    manager = BucketManager(test_config, embedding_engine=semantic)

    for case in cases:
        trace = {}
        results = await manager.search(
            case["query"],
            limit=len(buckets),
            candidate_buckets=buckets,
            trace=trace,
        )
        result_ids = [result["id"] for result in results]

        assert case["relevant"] in result_ids, case["label"]
        assert case["unrelated"] not in result_ids or result_ids.index(
            case["relevant"]
        ) < result_ids.index(case["unrelated"]), case["label"]

        relevant_entry = next(
            entry
            for entry in trace["candidates"]
            if entry["id"] == case["relevant"]
        )
        assert relevant_entry["scores"]["semantic"] == case["semantic_score"]
        assert relevant_entry["semantic_threshold"] is True
        assert relevant_entry["admitted"] is True

    assert len(semantic.calls) == len(cases)
    assert all(call[1] == 50 for call in semantic.calls)


def test_lexical_normalization_preserves_cjk_english_and_mixed_cues(test_config):
    manager = BucketManager(test_config)

    assert manager._normalize_search_text(" Python 性能优化 ") == "python性能优化"
    assert manager._normalize_search_text("中文 English") == "中文english"
    assert manager._normalize_search_text("雨") == "雨"

    mixed_bucket = _bucket(
        "mixed",
        name="Python 性能优化",
        keywords=["Python 性能优化"],
        content="profiling",
    )
    assert manager._calc_exact_match_score("Python 性能优化", mixed_bucket) == 1.0

