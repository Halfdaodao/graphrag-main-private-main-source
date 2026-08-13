from __future__ import annotations

import importlib.util
from pathlib import Path


SERVER_PATH = Path(__file__).resolve().parents[3] / "demo_frontend" / "server.py"
SPEC = importlib.util.spec_from_file_location("demo_frontend_server", SERVER_PATH)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


def test_extract_report_ids_ignores_more_and_deduplicates() -> None:
    answer = (
        "First [Data: Reports (10, 57, 123)] "
        "then [Data: Reports (9, 31, +more)] "
        "and again [Data: Reports (10)]."
    )

    assert SERVER._extract_report_ids(answer) == [10, 57, 123, 9, 31]


def test_extract_source_ids_ignores_more_and_deduplicates() -> None:
    answer = (
        "First [Data: Sources (2, 7, 64)] "
        "then [Data: Source (9, +more)] "
        "and again [Data: Sources (2)]."
    )

    assert SERVER._extract_source_ids(answer) == [2, 7, 64, 9]


def test_extract_source_ids_preserves_evidence_uuid() -> None:
    answer = (
        "[Data: Sources (811c42a7-aa7e-5235-af29-61248c29af29)] "
        "[Data: Sources (811c42a7-aa7e-5235-af29-61248c29af29, 2)]"
    )

    assert SERVER._extract_source_ids(answer) == [
        "811c42a7-aa7e-5235-af29-61248c29af29",
        2,
    ]


def test_resolve_report_123_to_current_euos_evidence() -> None:
    answer = "Data layer facts [Data: Reports (123)]."

    evidence, coverage = SERVER._resolve_answer_evidence(
        answer,
        "企业知识体系总体架构中的 Data 层包含哪些业务系统？",
    )

    assert coverage["report_ids"] == [123]
    assert coverage["resolved_report_ids"] == [123]
    assert coverage["unresolved_report_ids"] == []
    assert any(
        item["evidence_id"] == "811c42a7-aa7e-5235-af29-61248c29af29"
        for item in evidence
    )
    assert any(
        citation["report_id"] == 123 and citation["match_type"] == "exact"
        for item in evidence
        for citation in item["citations"]
    )


def test_resolver_maps_source_2_to_current_euos_evidence() -> None:
    evidence, coverage = SERVER._resolve_answer_evidence(
        "Data layer facts [Data: Sources (2)].",
        "企业知识体系总体架构中的 Data 层包含哪些业务系统？",
    )

    assert coverage["source_ids"] == [2]
    assert coverage["resolved_source_ids"] == [2]
    assert coverage["unresolved_source_ids"] == []
    assert evidence
    assert any(
        item["evidence_id"] == "811c42a7-aa7e-5235-af29-61248c29af29"
        for item in evidence
    )


def test_resolver_maps_evidence_uuid_source_to_current_euos_evidence() -> None:
    evidence, coverage = SERVER._resolve_answer_evidence(
        "Data layer facts [Data: Sources (811c42a7-aa7e-5235-af29-61248c29af29)].",
        "企业知识体系总体架构中的 Data 层包含哪些业务系统？",
    )

    assert coverage["source_ids"] == [
        "811c42a7-aa7e-5235-af29-61248c29af29"
    ]
    assert coverage["resolved_source_ids"] == [
        "811c42a7-aa7e-5235-af29-61248c29af29"
    ]
    assert coverage["unresolved_source_ids"] == []
    assert len(evidence) == 1
    assert evidence[0]["evidence_id"] == "811c42a7-aa7e-5235-af29-61248c29af29"


def test_resolver_aggregates_duplicate_evidence_across_reports() -> None:
    answer = "Shared facts [Data: Reports (57, 123)]."

    evidence, coverage = SERVER._resolve_answer_evidence(
        answer,
        "Data 层包含哪些业务系统？",
    )

    matching = [
        item
        for item in evidence
        if item["evidence_id"] == "811c42a7-aa7e-5235-af29-61248c29af29"
    ]
    assert len(matching) == 1
    assert set(matching[0]["report_ids"]) == {57, 123}
    assert {citation["match_type"] for citation in matching[0]["citations"]} == {"exact"}
    assert coverage["unique_evidence_count"] == len(evidence)


def test_resolver_does_not_fallback_to_multi_evidence_wiki_page() -> None:
    answer = "Broad facts [Data: Reports (113)]."

    evidence, coverage = SERVER._resolve_answer_evidence(
        answer,
        "一个没有明确对应章节的问题",
    )

    assert evidence == []
    assert coverage["resolved_report_ids"] == []
    assert coverage["unresolved_report_ids"] == [113]


def test_resolver_handles_answer_without_report_citations() -> None:
    evidence, coverage = SERVER._resolve_answer_evidence("No citations.", "question")

    assert evidence == []
    assert coverage == {
        "report_ids": [],
        "resolved_report_ids": [],
        "unresolved_report_ids": [],
        "unique_evidence_count": 0,
    }


def test_clean_query_output_removes_progress_and_ansi_logs() -> None:
    output = (
        "文档事实 [Data: Sources (2)].\n"
        "\x1b[92mLiteLLM:WARNING\x1b[0m: ignored\n"
        " 20%|██        | 1/5 [00:03<00:15, 3.79s/it]\n"
    )

    assert SERVER._clean_query_output(output) == "文档事实 [Data: Sources (2)]."


def test_grounded_answer_keeps_traceable_report_expansion() -> None:
    evidence = [{
        "evidence_id": "evidence-1",
        "text": (
            "\u5efa\u8bae\u91c7\u7528\u516d\u5c42\u67b6\u6784:\n\n"
            "------------------------\n\n"
            "Knowledge\u5c42 LLM Wiki Ontology RAG Graph Memory\n\n"
            "------------------------\n\n"
            "Data\u5c42 ERP\n\nMES\n\nCRM\n\nOA\n\nPLM\n\nWMS "
            "\u6570\u636e\u5e93\n\n"
            "------------------------\n\n"
            "Storage\u5c42 Neo4j PostgreSQL Milvus Redis S3"
        ),
        "heading": "\u7b2c\u4e09\u5377 \u4f01\u4e1a\u77e5\u8bc6\u4f53\u7cfb"
        "\u603b\u4f53\u67b6\u6784",
        "report_ids": [],
        "source_ids": [2],
    }]
    answer = (
        "# Data \u5c42\n\n"
        "Data \u5c42\u5305\u542b ERP\u3001MES\u3001CRM\u3001OA\u3001PLM\u3001"
        "WMS \u548c\u6570\u636e\u5e93 [Data: Sources (2)]\u3002\n\n"
        "\u8fd9\u4e9b\u7cfb\u7edf\u901a\u5e38\u5bf9\u5e94\u4e0d\u540c\u4e1a\u52a1"
        "\u9886\u57df\uff0c\u5e76\u6784\u6210\u4f01\u4e1a\u7684\u6570\u636e\u57fa"
        "\u7840 [Data: Sources (2)]\u3002"
    )

    grounded = SERVER._evidence_grounded_answer(
        answer,
        evidence,
        "\u4f01\u4e1a\u77e5\u8bc6\u4f53\u7cfb\u603b\u4f53\u67b6\u6784\u4e2d\u7684 "
        "Data \u5c42\u5305\u542b\u54ea\u4e9b\u4e1a\u52a1\u7cfb\u7edf\uff1f",
    )

    assert grounded == answer
    assert "\u6587\u6863\u539f\u6587" not in grounded
    assert "[Data: Sources (2)]" in grounded


def test_grounded_answer_rejects_unreferenced_hallucination() -> None:
    evidence = [{
        "evidence_id": "evidence-1",
        "text": "Data层 ERP MES CRM",
        "heading": "总体架构",
        "report_ids": [],
        "source_ids": [2],
    }]

    answer = SERVER._evidence_grounded_answer(
        "ERP \u8d1f\u8d23\u8d22\u52a1\u7ba1\u7406\u5e76\u786e\u4fdd\u4f01\u4e1a"
        "\u8d44\u6e90\u9ad8\u6548\u6d41\u8f6c\u3002",
        evidence,
        "ERP \u7684\u4f5c\u7528\u662f\u4ec0\u4e48\uff1f",
    )

    assert "\u8d1f\u8d23\u8d22\u52a1\u7ba1\u7406" not in answer
    assert "\u786e\u4fdd\u4f01\u4e1a\u8d44\u6e90" not in answer
    assert answer == "\u5f53\u524d\u8d44\u6599\u672a\u8bf4\u660e\uff0c\u65e0\u6cd5\u786e\u8ba4\u3002"


def test_grounded_answer_requires_resolved_evidence() -> None:
    assert SERVER._evidence_grounded_answer(
        "模型自行回答 [Data: Sources (99)]。",
        [],
    ) == "当前资料未说明，无法确认。"


def test_filter_answer_evidence_removes_broad_basic_search_matches() -> None:
    evidence = [
        {
            "evidence_id": "correct",
            "heading": "第三卷 企业知识体系总体架构",
            "text": "Data层 ERP MES CRM OA PLM WMS 数据库",
        },
        {
            "evidence_id": "unrelated-positioning",
            "heading": "第一卷 产品定位",
            "text": "企业知识库不是文档仓库。",
        },
        {
            "evidence_id": "unrelated-types",
            "heading": "第四卷 企业知识五大组成",
            "text": "企业知识包括文档知识、业务知识、经验知识、工具知识和实时知识。",
        },
    ]

    filtered = SERVER._filter_answer_evidence(
        evidence,
        "企业知识体系总体架构中的 Data 层包含哪些业务系统？",
    )

    assert [item["evidence_id"] for item in filtered] == ["correct"]


def test_filter_answer_evidence_rejects_unanswered_detail_question() -> None:
    evidence = [{
        "evidence_id": "data-layer",
        "heading": "第三卷 企业知识体系总体架构",
        "text": "Data层 ERP MES CRM OA PLM WMS 数据库",
    }]

    filtered = SERVER._filter_answer_evidence(
        evidence,
        "Data 层的 ERP 使用哪个数据库版本？",
    )

    assert filtered == []
