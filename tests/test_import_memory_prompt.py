from import_memory import IMPORT_EXTRACT_PROMPT


def test_preserve_raw_prompt_describes_extracted_content_bypass_not_transcript():
    prompt = IMPORT_EXTRACT_PROMPT

    assert "提取结果中的 content" in prompt
    assert "跳过后续合并/脱水摘要" in prompt
    assert "上传文件原文" in prompt
    assert "不可变来源证据" in prompt
    assert "保留原文不摘要" not in prompt
