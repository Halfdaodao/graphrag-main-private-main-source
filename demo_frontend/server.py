"""Small browser UI for exercising the local GraphRAG project."""

from __future__ import annotations

import json
import base64
from datetime import UTC, datetime
import hashlib
import hmac
import os
import re
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
STATIC = Path(__file__).resolve().parent
MODULE3_ROOT = ROOT / "module3_workspace"
EVIDENCE_DIR = MODULE3_ROOT / "evidence"
WIKI_DIR = MODULE3_ROOT / "wiki"
TEST_CASES_ROOT = MODULE3_ROOT / "test_cases"
GRAPH_INPUT_DIR = MODULE3_ROOT / "graph_input"
MANIFEST_PATH = MODULE3_ROOT / "manifest.json"
ACTIVE_EUOS_SOURCE_PATH = MODULE3_ROOT / "active_euos_source.json"
GOVERNANCE_DIR = MODULE3_ROOT / "governance"
GRAPH_PROFILES_PATH = GOVERNANCE_DIR / "graph_profiles.json"
RESOLUTIONS_PATH = GOVERNANCE_DIR / "entity_resolutions.json"
OBJECT_CATALOG_PATH = GOVERNANCE_DIR / "object_catalog.json"
OBJECT_MAPPINGS_PATH = GOVERNANCE_DIR / "object_mappings.json"
SNAPSHOTS_PATH = GOVERNANCE_DIR / "graph_snapshots.json"
BUILDS_PATH = GOVERNANCE_DIR / "graph_builds.json"
NEO4J_URI_DEFAULT = "bolt://127.0.0.1:7687"
NEO4J_USER_DEFAULT = "neo4j"
NEO4J_DATABASE_DEFAULT = "neo4j"
EUOS_KNOWLEDGE_URL_DEFAULT = "http://127.0.0.1:8090"
ALLOWED_METHODS = {"global", "local", "basic", "drift"}
SECRET_PATTERN = re.compile(r"\b(?:sk|sk-proj)-[A-Za-z0-9_-]+\b")
REVIEW_STATUSES = {"Candidate", "Accepted", "Rejected", "Stale"}
MAPPING_STATUSES = {"Candidate", "Accepted", "Rejected", "Stale"}
RESOLUTION_STATUSES = {"Candidate", "Accepted", "Rejected"}
INDEX_LOCK = Lock()

DEFAULT_GRAPH_PROFILE = {
    "id": "profile:maintenance-default",
    "name": "设备维护知识图谱",
    "version": 1,
    "active": True,
    "entityTypes": [
        "COMPONENT", "CONCEPT", "DOCUMENT", "EQUIPMENT", "EVENT", "FAILURE",
        "LOCATION", "MATERIAL", "ORGANIZATION", "OTHER", "PERSON", "PROCEDURE",
        "PRODUCT", "SAFETY_CONDITION", "SUPPLIER",
    ],
    "relationTypes": ["RELATED"],
    "reviewPolicy": {
        "requireEvidenceForRelations": True,
        "requireSchemaMatchForPublish": True,
    },
}

DEFAULT_OBJECT_CATALOG = [
    {
        "id": "object:equipment:elevator-001",
        "type": "EquipmentInstance",
        "name": "演示电梯 001",
        "externalRef": "ELEVATOR-001",
        "status": "Active",
    },
    {
        "id": "object:equipment:engine-1104d-001",
        "type": "EquipmentInstance",
        "name": "Perkins 1104D 演示发动机",
        "externalRef": "ENGINE-1104D-001",
        "status": "Active",
    },
    {
        "id": "object:workorder:maintenance-001",
        "type": "WorkOrder",
        "name": "例行维护工单 001",
        "externalRef": "WO-001",
        "status": "Open",
    },
]


def _load_local_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # The project-local configuration is authoritative for this service.
        # This prevents stale parent-process values, especially credentials,
        # from silently overriding a newly updated .env file.
        os.environ[key.strip()] = value.strip().strip('"')


_load_local_env()


def run_cli(args: list[str], timeout: int = 1800) -> tuple[int, str]:
    # Use the project's interpreter directly. In a Windows background process,
    # invoking ``uv run`` can inherit workspace arguments from the shell and
    # make the GraphRAG CLI see unrelated directory names as query arguments.
    python = ROOT / ".venv" / "Scripts" / "python.exe"
    command = [str(python), "-m", "graphrag", *args]
    env = os.environ.copy()
    # Windows defaults redirected Python output to the active console code page
    # (commonly GBK). The web API always transports UTF-8 JSON, so keep the
    # child process in UTF-8 as well.
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    completed = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    output = SECRET_PATTERN.sub("sk-***REDACTED***", output)
    return completed.returncode, output


def embedding_service_status() -> tuple[bool, str]:
    """Verify that the local OpenAI-compatible embedding endpoint can embed text."""
    base_url = "http://127.0.0.1:8001"
    try:
        health_request = Request(f"{base_url}/health", headers={"Accept": "application/json"})
        with urlopen(health_request, timeout=10) as response:  # noqa: S310 - fixed local endpoint
            health = json.loads(response.read().decode("utf-8"))
        if health.get("status") != "ok":
            return False, "嵌入服务健康检查未通过。"

        payload = json.dumps({
            "model": "BAAI/bge-m3",
            "input": "GraphRAG 索引健康检查",
        }).encode("utf-8")
        embedding_request = Request(
            f"{base_url}/v1/embeddings",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urlopen(embedding_request, timeout=60) as response:  # noqa: S310 - fixed local endpoint
            embedding = json.loads(response.read().decode("utf-8"))
        vectors = embedding.get("data") or []
        if not vectors or not vectors[0].get("embedding"):
            return False, "嵌入服务没有返回向量。"
    except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        return False, f"无法连接本地嵌入服务 http://127.0.0.1:8001：{exc}"
    return True, "本地 BGE-M3 嵌入服务正常。"


def _index_failure_reason(output: str) -> str | None:
    """Return the most useful GraphRAG failure line, if the pipeline reported one."""
    failure_markers = (
        "Pipeline error:",
        "Failed to validate embedding model",
        "Embedding configuration error detected",
        "Connection error",
        "Traceback",
    )
    matches = [
        line.strip()
        for line in output.splitlines()
        if any(marker in line for marker in failure_markers)
    ]
    return matches[-1] if matches else None


def _has_complete_index_outputs() -> tuple[bool, list[str]]:
    required = {"entities.parquet", "relationships.parquet", "text_units.parquet"}
    output_dir = ROOT / "output"
    files = sorted(path.name for path in output_dir.glob("*.parquet")) if output_dir.exists() else []
    return required.issubset(files), files


def _compact_index_output(output: str) -> str:
    """Keep the browser log useful without dumping GraphRAG dataframes."""
    workflows: list[str] = []
    warnings: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("Starting workflow:"):
            name = line.split(":", 1)[1].strip()
            if name not in workflows:
                workflows.append(name)
        elif line.startswith("Workflow complete:"):
            name = line.split(":", 1)[1].strip()
            if name not in workflows:
                workflows.append(name)
        elif (
            "WARNING" in line
            or "ERROR" in line
            or "Traceback" in line
            or "Pipeline error:" in line
            or "Failed to validate embedding model" in line
        ):
            warnings.append(line)
        elif "Pipeline complete" in line:
            workflows.append("Pipeline complete")
    failure = _index_failure_reason(output)
    lines = ["GraphRAG 索引失败。" if failure else "GraphRAG 索引执行结束。"]
    if workflows:
        lines.append("完成阶段：" + " → ".join(workflows))
    if failure:
        lines.append("失败原因：" + failure)
    elif warnings:
        lines.append("注意：")
        lines.extend(warnings[-5:])
    return "\n".join(lines)


def _ensure_module3_dirs() -> None:
    for directory in (
        EVIDENCE_DIR,
        WIKI_DIR,
        GRAPH_INPUT_DIR,
        TEST_CASES_ROOT / "module1",
        TEST_CASES_ROOT / "module2",
        GOVERNANCE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if not GRAPH_PROFILES_PATH.exists():
        _write_json(GRAPH_PROFILES_PATH, [DEFAULT_GRAPH_PROFILE])
    if not OBJECT_CATALOG_PATH.exists():
        _write_json(OBJECT_CATALOG_PATH, DEFAULT_OBJECT_CATALOG)
    for path in (RESOLUTIONS_PATH, OBJECT_MAPPINGS_PATH, SNAPSHOTS_PATH, BUILDS_PATH):
        if not path.exists():
            _write_json(path, [])


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"治理数据文件无效：{path.name}") from exc


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _governance_items(path: Path) -> list[dict]:
    _ensure_module3_dirs()
    items = _read_json(path, [])
    if not isinstance(items, list):
        raise ValueError(f"治理数据文件格式错误：{path.name}")
    return [item for item in items if isinstance(item, dict)]


def _active_graph_profile() -> dict:
    profiles = _governance_items(GRAPH_PROFILES_PATH)
    active = next((item for item in profiles if item.get("active")), None)
    if active is None:
        raise ValueError("没有启用的 Graph Profile")
    return active


def list_graph_profiles() -> list[dict]:
    return _governance_items(GRAPH_PROFILES_PATH)


def save_graph_profile(data: dict) -> dict:
    name = str(data.get("name") or "").strip()
    entity_types = sorted({str(value).strip().upper() for value in data.get("entityTypes", []) if str(value).strip()})
    relation_types = sorted({str(value).strip().upper() for value in data.get("relationTypes", []) if str(value).strip()})
    if not name or not entity_types or not relation_types:
        raise ValueError("Profile 名称、实体类型和关系类型不能为空")
    profiles = list_graph_profiles()
    profile_id = str(data.get("id") or _stable_id("profile", name.casefold()))
    old = next((item for item in profiles if item.get("id") == profile_id), {})
    profile = {
        "id": profile_id,
        "name": name,
        "version": int(old.get("version", 0)) + 1,
        "active": bool(data.get("active", True)),
        "entityTypes": entity_types,
        "relationTypes": relation_types,
        "reviewPolicy": {
            "requireEvidenceForRelations": bool(data.get("requireEvidenceForRelations", True)),
            "requireSchemaMatchForPublish": bool(data.get("requireSchemaMatchForPublish", True)),
        },
        "updatedAt": _now_iso(),
    }
    profiles = [item for item in profiles if item.get("id") != profile_id]
    if profile["active"]:
        for item in profiles:
            item["active"] = False
    profiles.append(profile)
    _write_json(GRAPH_PROFILES_PATH, profiles)
    return profile


def list_object_catalog() -> list[dict]:
    return _governance_items(OBJECT_CATALOG_PATH)


def list_entity_resolutions() -> list[dict]:
    return _governance_items(RESOLUTIONS_PATH)


def save_entity_resolution(data: dict) -> dict:
    entity_id = str(data.get("entityId") or "").strip()
    canonical_name = str(data.get("canonicalName") or "").strip()
    reviewer = str(data.get("reviewer") or "").strip()
    status = str(data.get("status") or "Accepted").strip()
    aliases = sorted({str(value).strip() for value in data.get("aliases", []) if str(value).strip()})
    if not entity_id or not canonical_name or not reviewer or status not in RESOLUTION_STATUSES:
        raise ValueError("entityId、canonicalName、reviewer 和合法 status 为必填项")
    resolutions = list_entity_resolutions()
    resolution = {
        "id": _stable_id("resolution", entity_id),
        "entityId": entity_id,
        "canonicalName": canonical_name,
        "aliases": aliases,
        "status": status,
        "reviewer": reviewer,
        "reason": str(data.get("reason") or "").strip(),
        "reviewedAt": _now_iso(),
    }
    resolutions = [item for item in resolutions if item.get("entityId") != entity_id]
    resolutions.append(resolution)
    _write_json(RESOLUTIONS_PATH, resolutions)

    config = _neo4j_config(data)
    if config["password"]:
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
            try:
                with driver.session(database=config["database"]) as session:
                    session.run(
                        """
                        MATCH (e:ExtractedEntity {id:$id})
                        SET e.canonical_name=$canonical_name, e.aliases=$aliases,
                            e.resolution_status=$status, e.resolution_reviewer=$reviewer,
                            e.resolution_reason=$reason, e.resolved_at=$reviewed_at,
                            e.updated_at=datetime()
                        """,
                        id=entity_id,
                        canonical_name=canonical_name,
                        aliases=aliases,
                        status=status,
                        reviewer=reviewer,
                        reason=resolution["reason"],
                        reviewed_at=resolution["reviewedAt"],
                    ).consume()
                    _refresh_published_projection(session)
            finally:
                driver.close()
        except Exception:
            # The decision remains durable locally; it is projected on the next successful sync.
            pass
    return resolution


def list_object_mappings() -> list[dict]:
    return _governance_items(OBJECT_MAPPINGS_PATH)


def save_object_mapping(data: dict) -> dict:
    entity_id = str(data.get("entityId") or "").strip()
    object_id = str(data.get("objectId") or "").strip()
    reviewer = str(data.get("reviewer") or "").strip()
    status = str(data.get("status") or "Candidate").strip()
    if not entity_id or not object_id or not reviewer or status not in MAPPING_STATUSES:
        raise ValueError("entityId、objectId、reviewer 和合法 status 为必填项")
    object_item = next((item for item in list_object_catalog() if item.get("id") == object_id), None)
    if object_item is None:
        raise ValueError("业务对象不存在")
    mappings = list_object_mappings()
    mapping = {
        "id": _stable_id("object-mapping", entity_id, object_id),
        "entityId": entity_id,
        "objectId": object_id,
        "objectName": object_item["name"],
        "objectType": object_item["type"],
        "status": status,
        "reviewer": reviewer,
        "reason": str(data.get("reason") or "").strip(),
        "reviewedAt": _now_iso(),
    }
    mappings = [item for item in mappings if item.get("id") != mapping["id"]]
    mappings.append(mapping)
    _write_json(OBJECT_MAPPINGS_PATH, mappings)
    config = _neo4j_config(data)
    if config["password"]:
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
            try:
                with driver.session(database=config["database"]) as session:
                    session.run(
                        """
                        MATCH (e:ExtractedEntity {id:$entity_id})
                        MERGE (o:BusinessObject {id:$object_id})
                        SET o.name=$object_name, o.object_type=$object_type, o.graph_origin='module3',
                            o.updated_at=datetime()
                        MERGE (e)-[r:OBJECT_MAPPING {id:$id}]->(o)
                        SET r.status=$status, r.reviewer=$reviewer, r.review_reason=$reason,
                            r.reviewed_at=$reviewed_at, r.graph_origin='module3', r.updated_at=datetime()
                        """,
                        entity_id=entity_id,
                        object_id=object_id,
                        object_name=mapping["objectName"],
                        object_type=mapping["objectType"],
                        id=mapping["id"],
                        status=status,
                        reviewer=reviewer,
                        reason=mapping["reason"],
                        reviewed_at=mapping["reviewedAt"],
                    ).consume()
            finally:
                driver.close()
        except Exception:
            pass
    return mapping


def _record_snapshot(snapshot_id: str, details: dict) -> dict:
    snapshots = _governance_items(SNAPSHOTS_PATH)
    record = {
        "id": snapshot_id,
        "status": "Ready",
        "createdAt": _now_iso(),
        "profileId": _active_graph_profile()["id"],
        **details,
    }
    snapshots = [item for item in snapshots if item.get("id") != snapshot_id]
    snapshots.append(record)
    _write_json(SNAPSHOTS_PATH, snapshots)
    return record


def _active_euos_source() -> dict:
    if not ACTIVE_EUOS_SOURCE_PATH.exists():
        return {}
    try:
        return json.loads(ACTIVE_EUOS_SOURCE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _current_graph_snapshot_id(manifest: dict) -> str:
    snapshot_key = json.dumps({
        "input": manifest.get("input", {}),
        "euosSource": manifest.get("euosSource") or _active_euos_source(),
    }, ensure_ascii=False, sort_keys=True)
    return f"module3:{hashlib.sha1(snapshot_key.encode('utf-8')).hexdigest()[:12]}"


def list_graph_snapshots() -> list[dict]:
    return sorted(_governance_items(SNAPSHOTS_PATH), key=lambda item: str(item.get("createdAt", "")), reverse=True)


def graph_quality_report() -> dict:
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    profile = _active_graph_profile()
    valid_entity_types = set(profile["entityTypes"])
    entity_type_counts: dict[str, int] = {}
    for row in entity_rows:
        entity_type = str(row.get("type") or "OTHER").upper()
        entity_type_counts[entity_type] = entity_type_counts.get(entity_type, 0) + 1
    resolutions = list_entity_resolutions()
    mappings = list_object_mappings()
    accepted_mappings = sum(item.get("status") == "Accepted" for item in mappings)
    return {
        "profileId": profile["id"],
        "entities": len(entity_rows),
        "relationships": len(relation_rows),
        "entityTypeCounts": entity_type_counts,
        "unknownEntityTypes": sorted(entity_type for entity_type in entity_type_counts if entity_type not in valid_entity_types),
        "resolvedEntities": sum(item.get("status") == "Accepted" for item in resolutions),
        "objectMappings": len(mappings),
        "acceptedObjectMappings": accepted_mappings,
        "snapshots": len(list_graph_snapshots()),
        "generatedAt": _now_iso(),
    }


def _safe_slug(value: str, fallback: str = "page") -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff.-]+", "-", value, flags=re.UNICODE).strip("-.")
    return (slug[:80] or fallback).lower()


def _read_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) != 3:
        return {}, text
    metadata: dict = {}
    for line in parts[1].splitlines():
        match = re.match(r"^([A-Za-z][\w-]*):\s*(.*)$", line.strip())
        if not match:
            continue
        key, value = match.groups()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            try:
                metadata[key] = json.loads(value.replace("'", '"'))
            except json.JSONDecodeError:
                metadata[key] = [item.strip().strip('"\'') for item in value[1:-1].split(",") if item.strip()]
        else:
            metadata[key] = value.strip('"\'')
    return metadata, parts[2].lstrip("\r\n")


def _load_wiki_page(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        page = json.loads(path.read_text(encoding="utf-8"))
        page["_source_file"] = path.name
        return page
    metadata, body = _read_frontmatter(path.read_text(encoding="utf-8"))
    title = metadata.get("title") or path.stem
    related = metadata.get("related", [])
    links = [{"target": item, "linkType": "RELATED"} for item in related]
    return {
        "contractVersion": "prototype-markdown",
        "projectId": "module3-poc",
        "wikiPageId": f"wiki:{_safe_slug(str(title))}",
        "wikiPageVersion": 1,
        "pageType": str(metadata.get("type", "CONCEPT")).upper(),
        "lifecycleStatus": "PUBLISHED",
        "title": str(title),
        "summary": body.splitlines()[0].lstrip("# ") if body.strip() else "",
        "bodyMarkdown": body,
        "evidenceRefs": [],
        "links": links,
        "sourceNames": metadata.get("sources", []),
        "_source_file": path.name,
    }


def _load_evidence_units() -> tuple[list[dict], list[dict]]:
    snapshots: list[dict] = []
    units: list[dict] = []
    for path in sorted(EVIDENCE_DIR.glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        snapshots.append(payload)
        for unit in payload.get("evidenceUnits", []):
            units.append(unit)
    return snapshots, units


def _page_evidence_refs(page: dict, evidence_by_id: dict[str, dict]) -> list[dict]:
    """Merge contract references with EUOS Markdown evidence anchors."""

    refs: list[dict] = []
    seen: set[str] = set()

    def add_ref(evidence_id: object, snapshot_id: object = "") -> None:
        evidence_id = str(evidence_id or "").strip()
        if not evidence_id or evidence_id in seen or evidence_id not in evidence_by_id:
            return
        evidence = evidence_by_id[evidence_id]
        refs.append({
            "evidenceId": evidence_id,
            "evidenceSnapshotId": str(
                snapshot_id or evidence.get("evidenceSnapshotId") or evidence.get("snapshotId") or ""
            ),
        })
        seen.add(evidence_id)

    for ref in page.get("evidenceRefs") or []:
        if isinstance(ref, dict):
            add_ref(ref.get("evidenceId"), ref.get("evidenceSnapshotId") or ref.get("snapshotId"))

    body = str(page.get("bodyMarkdown") or "")
    anchor_pattern = re.compile(
        r"Evidence:\s*`(?P<evidence_id>[^`]+)`\s*\|\s*"
        r"evidence://(?P<snapshot_id>[^/\s]+)/(?P<uri_evidence_id>[^#\s]+)",
        re.IGNORECASE,
    )
    for match in anchor_pattern.finditer(body):
        evidence_id = match.group("evidence_id").strip()
        if evidence_id == match.group("uri_evidence_id").strip():
            add_ref(evidence_id, match.group("snapshot_id"))

    for link in page.get("links") or []:
        evidence_ids = link.get("evidenceIds") or []
        if isinstance(evidence_ids, str):
            evidence_ids = re.split(r"[\s,]+", evidence_ids.strip())
        for evidence_id in evidence_ids:
            add_ref(evidence_id)
    return refs


def euos_connection_status() -> dict:
    """Return connection metadata without ever exposing the service token."""

    return {
        "url": (os.environ.get("EUOS_KNOWLEDGE_URL") or EUOS_KNOWLEDGE_URL_DEFAULT).rstrip("/"),
        "tokenConfigured": bool(
            os.environ.get("EUOS_SERVICE_TOKEN") or os.environ.get("EUOS_SERVICE_TOKEN_SHARED_SECRET")
        ),
    }


def _euos_service_token() -> str:
    token = os.environ.get("EUOS_SERVICE_TOKEN", "").strip()
    if token:
        return token
    secret = os.environ.get("EUOS_SERVICE_TOKEN_SHARED_SECRET", "").strip()
    if not secret:
        raise ValueError(
            "EUOS service authentication is not configured. Set EUOS_SERVICE_TOKEN or "
            "EUOS_SERVICE_TOKEN_SHARED_SECRET in the local .env file"
        )
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": os.environ.get("EUOS_SERVICE_TOKEN_ISSUER", "euos-internal"),
        "sub": f"service:{os.environ.get('EUOS_MODULE3_SERVICE_ID', 'graphrag-module3')}",
        "aud": os.environ.get("EUOS_SERVICE_TOKEN_AUDIENCE", "euos-internal"),
        "iat": now,
        "exp": now + int(os.environ.get("EUOS_SERVICE_TOKEN_TTL_SECONDS", "120")),
        "jti": hashlib.sha1(f"{now}:{os.urandom(16).hex()}".encode()).hexdigest(),
        "serviceId": os.environ.get("EUOS_MODULE3_SERVICE_ID", "graphrag-module3"),
    }
    encode = lambda value: base64.urlsafe_b64encode(  # noqa: E731
        json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    signing_input = f"{encode(header)}.{encode(payload)}"
    signature = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{signing_input}.{signature}"


def _euos_get(path: str, params: dict[str, object], project_id: str) -> dict:
    config = euos_connection_status()
    token = _euos_service_token()
    query = urlencode({key: value for key, value in params.items() if value is not None})
    url = f"{config['url']}{path}?{query}" if query else f"{config['url']}{path}"
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "X-Project-Id": project_id,
            "X-Trace-Id": f"module3-{hashlib.sha1(_now_iso().encode()).hexdigest()[:16]}",
            "X-EUOS-Service-Token": token,
        },
        method="GET",
    )
    try:
        with urlopen(request, timeout=30) as response:  # noqa: S310 - local configured service endpoint
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"EUOS request failed ({exc.code}): {detail[:500]}") from exc
    except URLError as exc:
        raise ValueError(f"Cannot connect to EUOS Knowledge API at {config['url']}: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise ValueError("EUOS returned a non-object JSON response")
    if payload.get("ok") is False:
        raise ValueError(str(payload.get("error") or payload.get("message") or "EUOS request was rejected"))
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise ValueError("EUOS response does not contain an object data payload")
    return data


def _write_euos_wiki_snapshot(snapshot: dict, project_id: str, wiki_space_id: str) -> int:
    """Normalize an immutable published Wiki snapshot to the local WikiPage contract."""

    pages = snapshot.get("pages") or []
    links_by_source: dict[str, list[dict]] = {}
    for link in snapshot.get("links") or []:
        source_id = str(link.get("sourcePageId") or "")
        target_id = str(link.get("targetPageId") or "")
        if source_id and target_id:
            links_by_source.setdefault(source_id, []).append({
                "targetWikiPageId": target_id,
                "linkType": str(link.get("relationType") or "RELATED_TO"),
                "confidence": link.get("confidence"),
                "evidenceIds": link.get("evidenceIds") or [],
            })
    written = 0
    for page in pages:
        page_id = str(page.get("pageId") or page.get("wikiPageId") or "")
        if not page_id:
            continue
        refs = []
        for ref in page.get("evidenceRefs") or []:
            evidence_id = str(ref.get("evidenceId") or "")
            snapshot_id = str(ref.get("evidenceSnapshotId") or ref.get("snapshotId") or "")
            if evidence_id and snapshot_id:
                refs.append({
                    "evidenceId": evidence_id,
                    "evidenceSnapshotId": snapshot_id,
                    "citationUri": ref.get("citationUri"),
                    "supportType": ref.get("supportType"),
                })
        local_page = {
            "contractVersion": "euos-published-wiki-1.0",
            "projectId": project_id,
            "wikiSpaceId": wiki_space_id,
            "wikiPageId": page_id,
            "wikiPageVersion": page.get("pageVersion") or page.get("wikiPageVersion") or 1,
            "wikiPageVersionId": page.get("pageVersionId"),
            "pageType": page.get("pageType") or "CONCEPT",
            "lifecycleStatus": page.get("lifecycleStatus") or "PUBLISHED",
            "freshnessStatus": page.get("freshnessStatus") or "UNKNOWN",
            "title": page.get("title") or page_id,
            "summary": page.get("summary") or "",
            "bodyMarkdown": page.get("bodyMarkdown") or "",
            "evidenceRefs": refs,
            "links": links_by_source.get(page_id, []),
            "relatedPageIds": page.get("relatedPageIds") or [],
            "sourceSnapshot": {
                "eventId": snapshot.get("eventId"),
                "wikiVersion": snapshot.get("wikiVersion"),
                "manifestSha256": snapshot.get("manifestSha256"),
            },
        }
        target = WIKI_DIR / f"euos-{_safe_slug(page_id)}.json"
        target.write_text(json.dumps(local_page, ensure_ascii=False, indent=2), encoding="utf-8")
        written += 1
    return written


def sync_from_euos(data: dict) -> dict:
    """Fetch published Wiki and its exact Evidence snapshots through EUOS APIs."""

    _ensure_module3_dirs()
    project_id = str(data.get("projectId") or "").strip()
    wiki_space_id = str(data.get("wikiSpaceId") or "").strip()
    if not project_id or not wiki_space_id:
        raise ValueError("projectId and wikiSpaceId are required")
    requested_version = data.get("wikiVersion")
    if requested_version in {"", None}:
        active = _euos_get(
            "/internal/v1/knowledge/wiki/publish/active",
            {"project_id": project_id, "wiki_space_id": wiki_space_id},
            project_id,
        )
        if active is None:
            raise ValueError("EUOS has no active published Wiki version for this project and space")
        requested_version = active.get("wikiVersion")
    try:
        wiki_version = int(requested_version)
    except (TypeError, ValueError) as exc:
        raise ValueError("wikiVersion must be a positive integer") from exc
    if wiki_version < 1:
        raise ValueError("wikiVersion must be a positive integer")

    # The workspace represents one published EUOS version at a time.
    # Clear only generated Module 3 inputs before writing the requested source.
    for directory in (EVIDENCE_DIR, WIKI_DIR, GRAPH_INPUT_DIR):
        for path in directory.iterdir():
            if path.is_file():
                path.unlink()

    snapshot = _euos_get(
        "/internal/v1/knowledge/wiki/published-snapshots",
        {"projectId": project_id, "wikiSpaceId": wiki_space_id, "wikiVersion": wiki_version},
        project_id,
    )
    snapshot_ids = set(str(item) for item in (snapshot.get("lineage") or {}).get("evidenceSnapshotIds", []) if item)
    for page in snapshot.get("pages") or []:
        for ref in page.get("evidenceRefs") or []:
            snapshot_id = ref.get("evidenceSnapshotId") or ref.get("snapshotId")
            if snapshot_id:
                snapshot_ids.add(str(snapshot_id))

    evidence_count = 0
    for snapshot_id in sorted(snapshot_ids):
        evidence_snapshot = _euos_get(
            f"/internal/v1/knowledge/evidence-snapshots/{snapshot_id}",
            {"projectId": project_id, "includeEvidence": "true"},
            project_id,
        )
        if evidence_snapshot.get("isTruncated"):
            units: list[dict] = []
            offset = 0
            while True:
                page = _euos_get(
                    f"/internal/v1/knowledge/evidence-snapshots/{snapshot_id}/evidence",
                    {"projectId": project_id, "offset": offset, "limit": 500},
                    project_id,
                )
                units.extend(page.get("items") or [])
                if not page.get("hasMore"):
                    break
                offset += int(page.get("limit") or 500)
            evidence_snapshot["evidenceUnits"] = units
            evidence_snapshot["evidenceCount"] = len(units)
            evidence_snapshot["isTruncated"] = False
        evidence_count += len(evidence_snapshot.get("evidenceUnits") or [])
        target = EVIDENCE_DIR / f"euos-{_safe_slug(snapshot_id)}.json"
        target.write_text(json.dumps(evidence_snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    wiki_count = _write_euos_wiki_snapshot(snapshot, project_id, wiki_space_id)
    return {
        "ok": True,
        "projectId": project_id,
        "wikiSpaceId": wiki_space_id,
        "wikiVersion": wiki_version,
        "eventId": snapshot.get("eventId"),
        "wikiPages": wiki_count,
        "evidenceSnapshots": len(snapshot_ids),
        "evidenceUnits": evidence_count,
    }


def prepare_module3_input() -> dict:
    _ensure_module3_dirs()
    output_dir = ROOT / "output"
    if output_dir.exists():
        for path in output_dir.glob("*.parquet"):
            path.unlink()
    snapshots, evidence_units = _load_evidence_units()
    evidence_by_id = {str(unit.get("evidenceId")): unit for unit in evidence_units}
    pages = [_load_wiki_page(path) for path in sorted(WIKI_DIR.iterdir()) if path.suffix.lower() in {".json", ".md"}]
    if not pages:
        raise ValueError("请先导入模块2 WikiPage JSON/Markdown")
    for old in GRAPH_INPUT_DIR.glob("*.md"):
        old.unlink()
    entries: list[dict] = []
    for index, page in enumerate(pages):
        title = str(page.get("title") or page.get("wikiPageId") or f"wiki-page-{index}")
        page_id = str(page.get("wikiPageId") or f"wiki:{_safe_slug(title)}")
        refs = _page_evidence_refs(page, evidence_by_id)
        linked_units = [evidence_by_id[ref["evidenceId"]] for ref in refs]
        links = page.get("links") or []
        lines = [
            f"# {title}",
            f"Wiki page id: {page_id}",
            f"Page type: {page.get('pageType', 'CONCEPT')}",
            f"Summary: {page.get('summary', '')}",
            "",
            str(page.get("bodyMarkdown") or ""),
        ]
        if links:
            lines.extend(["", "## Wiki links"])
            for link in links:
                target = link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or ""
                predicate = link.get("linkType") or link.get("predicate") or "RELATED_TO"
                lines.append(f"{title} -[{predicate}]-> {target}")
        if linked_units:
            lines.extend(["", "## Evidence from module 1"])
            for unit in linked_units:
                evidence_id = unit.get("evidenceId", "")
                lines.append(f"Evidence ID: {evidence_id}")
                lines.append(str(unit.get("text", "")))
                lines.append(f"Evidence heading: {' / '.join(unit.get('headingPath', []))}")
        output_name = f"{index:03d}-{_safe_slug(title)}.md"
        (GRAPH_INPUT_DIR / output_name).write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        entries.append({
            "graphInputFile": output_name,
            "wikiPageId": page_id,
            "wikiPageVersion": page.get("wikiPageVersion", 1),
            "title": title,
            "evidenceRefs": refs,
            "links": links,
        })
    manifest = {
        "contractVersion": "module3-poc-1.0",
        "input": {"evidenceSnapshots": [item.get("evidenceSnapshotId") for item in snapshots], "wikiPages": entries},
        "counts": {"evidenceSnapshots": len(snapshots), "evidenceUnits": len(evidence_units), "wikiPages": len(pages)},
        "euosSource": _active_euos_source(),
        "graphInputDir": str(GRAPH_INPUT_DIR),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _read_parquet_rows(name: str, limit: int = 100) -> list[dict]:
    path = ROOT / "output" / name
    if not path.exists():
        return []
    try:
        import pandas as pd
        return json.loads(pd.read_parquet(path).head(limit).to_json(orient="records", force_ascii=False, date_format="iso"))
    except Exception as exc:  # noqa: BLE001
        return [{"error": f"读取 {name} 失败: {exc}"}]


def _as_list(value: object) -> list[str]:
    """Normalize GraphRAG list columns across pandas/pyarrow versions."""
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item is not None]
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return []
    if text.startswith("["):
        quoted = re.findall(r"""['"]([^'"]+)['"]""", text)
        if quoted:
            return quoted
        try:
            parsed = json.loads(text)
            return [str(item) for item in parsed if item is not None]
        except json.JSONDecodeError:
            pass
    return [text]


def _stable_id(prefix: str, *parts: object) -> str:
    value = "|".join(str(part).strip() for part in parts)
    return f"{prefix}:{hashlib.sha1(value.encode('utf-8')).hexdigest()[:24]}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _neo4j_config(data: dict | None = None) -> dict:
    data = data or {}
    return {
        "uri": str(data.get("uri") or os.environ.get("NEO4J_URI") or NEO4J_URI_DEFAULT),
        "user": str(data.get("user") or os.environ.get("NEO4J_USERNAME") or NEO4J_USER_DEFAULT),
        "password": str(data.get("password") or os.environ.get("NEO4J_PASSWORD") or ""),
        "database": str(data.get("database") or os.environ.get("NEO4J_DATABASE") or NEO4J_DATABASE_DEFAULT),
    }


def _sync_neo4j_replace_all_legacy(data: dict) -> dict:
    """Legacy replace-all projection retained only for historical reference."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise ValueError("未安装 Neo4j Python 驱动，请先安装 neo4j") from exc
    config = _neo4j_config(data)
    if not config["password"]:
        raise ValueError("请输入 Neo4j 密码，密码不会写入日志或文件")
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    if not entity_rows:
        raise ValueError("没有找到实体结果，请先运行模块 3 索引")
    if entity_rows and "error" in entity_rows[0]:
        raise ValueError(entity_rows[0]["error"])
    if relation_rows and "error" in relation_rows[0]:
        raise ValueError(relation_rows[0]["error"])
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    input_key = json.dumps(manifest.get("input", {}), ensure_ascii=False, sort_keys=True)
    snapshot_hash = hashlib.sha1(input_key.encode("utf-8")).hexdigest()[:12]
    snapshot_id = f"module3:{manifest.get('contractVersion', 'unknown')}:{snapshot_hash}"
    nodes = []
    title_to_id = {}
    for row in entity_rows:
        title = str(row.get("title") or "unknown")
        stable_key = hashlib.sha1(title.casefold().strip().encode("utf-8")).hexdigest()[:20]
        entity_id = f"entity:{stable_key}"
        title_to_id[title] = entity_id
        nodes.append({
            "id": entity_id,
            "title": title,
            "entity_type": str(row.get("type") or "UNKNOWN"),
            "description": str(row.get("description") or ""),
            "frequency": row.get("frequency"),
            "degree": row.get("degree"),
            "graph_snapshot": snapshot_id,
        })
    edges = []
    for row in relation_rows:
        source_title = str(row.get("source") or "")
        target_title = str(row.get("target") or "")
        source_id = title_to_id.get(source_title) or f"entity:{_safe_slug(source_title, 'source')}"
        target_id = title_to_id.get(target_title) or f"entity:{_safe_slug(target_title, 'target')}"
        relation_key = "|".join((snapshot_id, source_id, target_id, str(row.get("description") or "")))
        edges.append({
            "id": f"module3:relation:{hashlib.sha1(relation_key.encode('utf-8')).hexdigest()[:20]}",
            "source_id": source_id,
            "target_id": target_id,
            "source_title": source_title,
            "target_title": target_title,
            "description": str(row.get("description") or ""),
            "weight": row.get("weight"),
            "combined_degree": row.get("combined_degree"),
            "graph_snapshot": snapshot_id,
        })
    wiki_nodes = []
    wiki_edges = []
    for page in manifest.get("input", {}).get("wikiPages", []):
        page_id = str(page.get("wikiPageId") or f"wiki:{_safe_slug(str(page.get('title', 'page')))}")
        wiki_nodes.append({
            "id": page_id,
            "title": str(page.get("title") or page_id),
            "page_version": page.get("wikiPageVersion", 1),
            "evidence_refs": json.dumps(page.get("evidenceRefs", []), ensure_ascii=False),
            "graph_snapshot": snapshot_id,
        })
        for index, link in enumerate(page.get("links", [])):
            target = str(link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or "")
            if not target:
                continue
            target_id = target if target.startswith("wiki:") else f"wiki:{_safe_slug(target, 'target')}"
            wiki_edges.append({
                "id": f"{page_id}:link:{index}:{target_id}",
                "source_id": page_id,
                "target_id": target_id,
                "target_title": target,
                "predicate": str(link.get("linkType") or link.get("predicate") or "RELATED"),
                "graph_snapshot": snapshot_id,
            })
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        driver.verify_connectivity()
        with driver.session(database=config["database"]) as session:
            session.run("""
                MATCH (n)
                WHERE n.graph_snapshot STARTS WITH 'module3:'
                DETACH DELETE n
            """).consume()
            session.run("""
                CREATE CONSTRAINT entity_id_unique IF NOT EXISTS
                FOR (n:Entity) REQUIRE n.id IS UNIQUE
            """).consume()
            session.run("""
                CREATE CONSTRAINT relation_id_unique IF NOT EXISTS
                FOR ()-[r:RELATED_TO]-() REQUIRE r.id IS UNIQUE
            """).consume()
            session.run("""
                CREATE CONSTRAINT wiki_page_id_unique IF NOT EXISTS
                FOR (n:WikiPage) REQUIRE n.id IS UNIQUE
            """).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (n:Entity {id: row.id})
                SET n.title = row.title,
                    n.entity_type = row.entity_type,
                    n.description = row.description,
                    n.frequency = row.frequency,
                    n.degree = row.degree,
                    n.graph_snapshot = row.graph_snapshot,
                    n.updated_at = datetime()
            """, rows=nodes).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (s:Entity {id: row.source_id})
                ON CREATE SET s.title = row.source_title, s.entity_type = 'UNKNOWN'
                MERGE (t:Entity {id: row.target_id})
                ON CREATE SET t.title = row.target_title, t.entity_type = 'UNKNOWN'
                MERGE (s)-[r:RELATED_TO {id: row.id}]->(t)
                SET r.description = row.description,
                    r.weight = row.weight,
                    r.combined_degree = row.combined_degree,
                    r.graph_snapshot = row.graph_snapshot,
                    r.updated_at = datetime()
            """, rows=edges).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (n:WikiPage {id: row.id})
                SET n.title = row.title,
                    n.page_version = row.page_version,
                    n.evidence_refs = row.evidence_refs,
                    n.graph_snapshot = row.graph_snapshot,
                    n.updated_at = datetime()
            """, rows=wiki_nodes).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (s:WikiPage {id: row.source_id})
                MERGE (t:WikiPage {id: row.target_id})
                ON CREATE SET t.title = row.target_title,
                              t.graph_snapshot = row.graph_snapshot
                MERGE (s)-[r:WIKI_LINK {id: row.id}]->(t)
                SET r.predicate = row.predicate,
                    r.graph_snapshot = row.graph_snapshot,
                    r.updated_at = datetime()
            """, rows=wiki_edges).consume()
        return {"ok": True, "uri": config["uri"], "database": config["database"], "nodes": len(nodes), "relationships": len(edges), "wiki_pages": len(wiki_nodes), "wiki_links": len(wiki_edges), "snapshot": snapshot_id}
    finally:
        driver.close()


def sync_neo4j(data: dict) -> dict:
    """Incrementally project module 1/2 provenance and the GraphRAG graph into Neo4j."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise ValueError("Neo4j Python driver is not installed") from exc

    config = _neo4j_config(data)
    if not config["password"]:
        raise ValueError("Neo4j password is not configured")
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    text_unit_rows = _read_parquet_rows("text_units.parquet", limit=100000)
    document_rows = _read_parquet_rows("documents.parquet", limit=100000)
    for rows in (entity_rows, relation_rows, text_unit_rows, document_rows):
        if rows and "error" in rows[0]:
            raise ValueError(rows[0]["error"])
    if not entity_rows:
        raise ValueError("No GraphRAG entity output found; run indexing first")
    if not MANIFEST_PATH.exists():
        raise ValueError("No module 3 manifest found; prepare input first")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pages = manifest.get("input", {}).get("wikiPages", [])
    page_by_id = {
        str(page.get("wikiPageId") or f"wiki:{_safe_slug(str(page.get('title', 'page')))}"): page
        for page in pages
    }
    page_ids = list(page_by_id)
    if not page_ids:
        raise ValueError("No WikiPage input found")
    input_key = json.dumps(manifest.get("input", {}), ensure_ascii=False, sort_keys=True)
    snapshot_id = f"module3:{hashlib.sha1(input_key.encode('utf-8')).hexdigest()[:12]}"

    snapshots, evidence_units = _load_evidence_units()
    snapshots_by_id = {str(row.get("evidenceSnapshotId")): row for row in snapshots}
    evidence_by_id = {str(row.get("evidenceId")): row for row in evidence_units}
    document_nodes: dict[str, dict] = {}
    evidence_nodes: dict[str, dict] = {}
    page_document_edges: list[dict] = []
    page_evidence_edges: list[dict] = []
    wiki_nodes: list[dict] = []

    for page_id, page in page_by_id.items():
        wiki_nodes.append({
            "id": page_id,
            "title": str(page.get("title") or page_id),
            "page_version": page.get("wikiPageVersion", 1),
            "evidence_refs": json.dumps(page.get("evidenceRefs", []), ensure_ascii=False),
            "graph_snapshot": snapshot_id,
        })
        for ref in page.get("evidenceRefs") or []:
            snapshot_id_raw = str(ref.get("evidenceSnapshotId") or "")
            evidence_id_raw = str(ref.get("evidenceId") or "")
            snapshot = snapshots_by_id.get(snapshot_id_raw, {})
            document = snapshot.get("document") or {}
            raw_document_id = str(document.get("documentId") or snapshot_id_raw)
            if raw_document_id:
                document_id = f"document:{raw_document_id}"
                document_nodes[document_id] = {
                    "id": document_id,
                    "document_id": raw_document_id,
                    "document_version_id": str(document.get("documentVersionId") or ""),
                    "title": str(document.get("title") or raw_document_id),
                    "category_code": str(document.get("categoryCode") or ""),
                    "graph_snapshot": snapshot_id,
                }
                page_document_edges.append({"id": f"has_wiki:{document_id}:{page_id}", "document_id": document_id, "page_id": page_id})
            evidence = evidence_by_id.get(evidence_id_raw, {})
            if evidence_id_raw:
                evidence_id = f"evidence:{evidence_id_raw}"
                evidence_nodes[evidence_id] = {
                    "id": evidence_id,
                    "evidence_id": evidence_id_raw,
                    "snapshot_id": snapshot_id_raw,
                    "heading": " / ".join(evidence.get("headingPath") or []),
                    "text": str(evidence.get("text") or ""),
                    "graph_snapshot": snapshot_id,
                }
                page_evidence_edges.append({"id": f"has_evidence:{page_id}:{evidence_id}", "page_id": page_id, "evidence_id": evidence_id})

    page_by_graph_file = {
        str(page.get("graphInputFile")): page_id for page_id, page in page_by_id.items()
    }
    document_to_pages: dict[str, set[str]] = {}
    for row in document_rows:
        page_id = page_by_graph_file.get(str(row.get("title") or ""))
        if page_id:
            document_to_pages.setdefault(str(row.get("id") or ""), set()).add(page_id)
            for text_unit_id in row.get("text_unit_ids") or []:
                document_to_pages.setdefault(str(text_unit_id), set()).add(page_id)
    text_unit_to_pages: dict[str, set[str]] = {}
    for row in text_unit_rows:
        page_ids_for_unit = document_to_pages.get(str(row.get("document_id") or ""), set())
        if page_ids_for_unit:
            text_unit_to_pages.setdefault(str(row.get("id") or ""), set()).update(page_ids_for_unit)

    entities: list[dict] = []
    title_to_id: dict[str, str] = {}
    mention_edges: list[dict] = []
    for row in entity_rows:
        title = str(row.get("title") or "unknown")
        entity_id = f"entity:{hashlib.sha1(title.casefold().strip().encode('utf-8')).hexdigest()[:20]}"
        title_to_id[title] = entity_id
        entities.append({
            "id": entity_id,
            "title": title,
            "entity_type": str(row.get("type") or "OTHER"),
            "description": str(row.get("description") or ""),
            "frequency": row.get("frequency"),
            "degree": row.get("degree"),
            "graph_snapshot": snapshot_id,
        })
        source_pages = set().union(*(text_unit_to_pages.get(str(unit_id), set()) for unit_id in row.get("text_unit_ids") or []))
        for page_id in source_pages:
            mention_edges.append({"id": f"mentions:{page_id}:{entity_id}", "page_id": page_id, "entity_id": entity_id})

    relation_edges: list[dict] = []
    for row in relation_rows:
        source_title = str(row.get("source") or "")
        target_title = str(row.get("target") or "")
        source_id = title_to_id.get(source_title) or f"entity:{_safe_slug(source_title, 'source')}"
        target_id = title_to_id.get(target_title) or f"entity:{_safe_slug(target_title, 'target')}"
        source_pages = set().union(*(text_unit_to_pages.get(str(unit_id), set()) for unit_id in row.get("text_unit_ids") or []))
        for page_id in source_pages:
            relation_key = "|".join((page_id, source_id, target_id, str(row.get("description") or "")))
            relation_edges.append({
                "id": f"module3:relation:{hashlib.sha1(relation_key.encode('utf-8')).hexdigest()[:20]}",
                "source_id": source_id,
                "target_id": target_id,
                "source_title": source_title,
                "target_title": target_title,
                "description": str(row.get("description") or ""),
                "weight": row.get("weight"),
                "combined_degree": row.get("combined_degree"),
                "wiki_page_id": page_id,
                "graph_snapshot": snapshot_id,
            })

    wiki_links: list[dict] = []
    for page_id, page in page_by_id.items():
        for index, link in enumerate(page.get("links") or []):
            target = str(link.get("targetWikiPageId") or link.get("target") or link.get("targetTitle") or "")
            if target:
                target_id = target if target in page_by_id else f"wiki:{_safe_slug(target, 'target')}"
                wiki_links.append({
                    "id": f"wiki_link:{page_id}:{index}:{target_id}", "source_id": page_id, "target_id": target_id,
                    "target_title": target, "predicate": str(link.get("linkType") or link.get("predicate") or "RELATED"),
                    "graph_snapshot": snapshot_id,
                })

    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        driver.verify_connectivity()
        with driver.session(database=config["database"]) as session:
            for statement in (
                "CREATE CONSTRAINT entity_id_unique IF NOT EXISTS FOR (n:Entity) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT wiki_page_id_unique IF NOT EXISTS FOR (n:WikiPage) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT document_id_unique IF NOT EXISTS FOR (n:SourceDocument) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT evidence_id_unique IF NOT EXISTS FOR (n:Evidence) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT relation_id_unique IF NOT EXISTS FOR ()-[r:RELATED_TO]-() REQUIRE r.id IS UNIQUE",
            ):
                session.run(statement).consume()
            # One-time migration cleanup for relationships created by the old replace-all implementation.
            session.run("MATCH ()-[r]->() WHERE r.graph_snapshot STARTS WITH 'module3:' AND r.projection_version IS NULL DELETE r").consume()
            session.run("MATCH (p:WikiPage)-[r:HAS_EVIDENCE|MENTIONS|WIKI_LINK]->() WHERE p.id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("MATCH (:SourceDocument)-[r:HAS_WIKI]->(p:WikiPage) WHERE p.id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("MATCH ()-[r:RELATED_TO]->() WHERE r.wiki_page_id IN $page_ids DELETE r", page_ids=page_ids).consume()
            session.run("UNWIND $rows AS row MERGE (n:SourceDocument {id: row.id}) SET n.document_id=row.document_id, n.document_version_id=row.document_version_id, n.title=row.title, n.category_code=row.category_code, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=list(document_nodes.values())).consume()
            session.run("UNWIND $rows AS row MERGE (n:Evidence {id: row.id}) SET n.evidence_id=row.evidence_id, n.snapshot_id=row.snapshot_id, n.heading=row.heading, n.text=row.text, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=list(evidence_nodes.values())).consume()
            session.run("UNWIND $rows AS row MERGE (n:WikiPage {id: row.id}) SET n.title=row.title, n.page_version=row.page_version, n.evidence_refs=row.evidence_refs, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=wiki_nodes).consume()
            session.run("UNWIND $rows AS row MERGE (n:Entity {id: row.id}) SET n.title=row.title, n.entity_type=row.entity_type, n.description=row.description, n.frequency=row.frequency, n.degree=row.degree, n.graph_origin='module3', n.projection_version='incremental-v2', n.graph_snapshot=row.graph_snapshot, n.updated_at=datetime()", rows=entities).consume()
            session.run("UNWIND $rows AS row MATCH (d:SourceDocument {id: row.document_id}) MATCH (p:WikiPage {id: row.page_id}) MERGE (d)-[r:HAS_WIKI {id: row.id}]->(p) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=page_document_edges).consume()
            session.run("UNWIND $rows AS row MATCH (p:WikiPage {id: row.page_id}) MATCH (e:Evidence {id: row.evidence_id}) MERGE (p)-[r:HAS_EVIDENCE {id: row.id}]->(e) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=page_evidence_edges).consume()
            session.run("UNWIND $rows AS row MATCH (p:WikiPage {id: row.page_id}) MATCH (e:Entity {id: row.entity_id}) MERGE (p)-[r:MENTIONS {id: row.id}]->(e) SET r.graph_origin='module3', r.projection_version='incremental-v2', r.updated_at=datetime()", rows=mention_edges).consume()
            session.run("UNWIND $rows AS row MERGE (s:Entity {id: row.source_id}) ON CREATE SET s.title=row.source_title, s.entity_type='OTHER', s.graph_origin='module3', s.projection_version='incremental-v2' MERGE (t:Entity {id: row.target_id}) ON CREATE SET t.title=row.target_title, t.entity_type='OTHER', t.graph_origin='module3', t.projection_version='incremental-v2' MERGE (s)-[r:RELATED_TO {id: row.id}]->(t) SET r.description=row.description, r.weight=row.weight, r.combined_degree=row.combined_degree, r.wiki_page_id=row.wiki_page_id, r.graph_origin='module3', r.projection_version='incremental-v2', r.graph_snapshot=row.graph_snapshot, r.updated_at=datetime()", rows=relation_edges).consume()
            session.run("UNWIND $rows AS row MERGE (s:WikiPage {id: row.source_id}) MERGE (t:WikiPage {id: row.target_id}) ON CREATE SET t.title=row.target_title, t.graph_origin='module3', t.projection_version='incremental-v2' MERGE (s)-[r:WIKI_LINK {id: row.id}]->(t) SET r.predicate=row.predicate, r.graph_origin='module3', r.projection_version='incremental-v2', r.graph_snapshot=row.graph_snapshot, r.updated_at=datetime()", rows=wiki_links).consume()
            session.run("MATCH (n:Evidence {graph_origin:'module3'}) WHERE NOT (n)<-[:HAS_EVIDENCE]-() DETACH DELETE n").consume()
            session.run("MATCH (n:SourceDocument {graph_origin:'module3'}) WHERE NOT (n)-[:HAS_WIKI]->() DETACH DELETE n").consume()
        return {"ok": True, "uri": config["uri"], "database": config["database"], "documents": len(document_nodes), "evidence": len(evidence_nodes), "wiki_pages": len(wiki_nodes), "entities": len(entities), "relationships": len(relation_edges), "snapshot": snapshot_id, "mode": "incremental-v2"}
    finally:
        driver.close()


def build_query_evidence(query: str, answer: str) -> dict:
    """Resolve answer terms back to the Neo4j provenance graph for the UI."""
    config = _neo4j_config({})
    if not config["password"]:
        return {"entities": [], "paths": [], "sources": []}
    _, units = _load_evidence_units()
    unit_by_id = {str(unit.get("evidenceId")): unit for unit in units}
    driver = None
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
        with driver.session(database=config["database"]) as session:
            entity_rows = list(session.run(
                "MATCH (n:Entity) WHERE $answer CONTAINS n.title OR $question CONTAINS n.title "
                "RETURN n.id AS id, n.title AS title, n.entity_type AS type LIMIT 12",
                parameters={"answer": answer, "question": query},
            ))
            entity_ids = [row["id"] for row in entity_rows]
            path_rows = list(session.run(
                "MATCH (w:WikiPage)-[:MENTIONS]->(s:Entity)-[r:RELATED_TO]->(t:Entity) "
                "WHERE s.id IN $entity_ids OR t.id IN $entity_ids "
                "RETURN w.title AS wiki_title, w.id AS wiki_page_id, s.title AS source, t.title AS target, "
                "r.description AS description, r.weight AS weight LIMIT 12",
                entity_ids=entity_ids,
            )) if entity_ids else []
            page_ids = list(dict.fromkeys(row["wiki_page_id"] for row in path_rows))
            source_query = (
                "MATCH (d:SourceDocument)-[:HAS_WIKI]->(w:WikiPage)-[:HAS_EVIDENCE]->(e:Evidence) "
                "WHERE w.id IN $page_ids "
                "RETURN d.title AS document_title, d.document_id AS document_id, d.document_version_id AS document_version_id, "
                "w.title AS wiki_title, w.id AS wiki_page_id, e.evidence_id AS evidence_id, e.heading AS heading, e.text AS text LIMIT 12"
            )
            source_rows = list(session.run(source_query, page_ids=page_ids)) if page_ids else []
    except Exception as exc:  # noqa: BLE001
        return {"entities": [], "paths": [], "sources": [], "error": str(exc)}
    finally:
        if driver is not None:
            driver.close()
    sources = []
    for row in source_rows:
        unit = unit_by_id.get(str(row["evidence_id"]), {})
        pages = sorted({str(item.get("pageNumber")) for item in unit.get("sourceFragments", []) if item.get("pageNumber") is not None})
        sources.append({
            **dict(row),
            "page_numbers": pages,
        })
    return {
        "entities": [dict(row) for row in entity_rows],
        "paths": [dict(row) for row in path_rows],
        "sources": sources,
    }


def _refresh_published_projection(session) -> dict:
    """Project only reviewed, evidence-backed candidates into the formal layer."""
    session.run(
        "MATCH (n:PublishedEntity {graph_origin:'module3'}) DETACH DELETE n"
    ).consume()
    session.run(
        """
        MATCH (c:EntityCandidate {graph_origin:'module3', status:'Accepted'})
              -[:DESCRIBES]->(e:ExtractedEntity)
        MERGE (p:PublishedEntity {id:'published:' + e.id})
        SET p.extracted_id=e.id, p.title=coalesce(e.canonical_name, e.title), p.aliases=coalesce(e.aliases, []), p.entity_type=e.entity_type,
            p.description=e.description, p.graph_origin='module3',
            p.candidate_id=c.id, p.status='Accepted', p.updated_at=datetime()
        """
    ).consume()
    session.run(
        """
        MATCH (c:RelationshipCandidate {graph_origin:'module3', status:'Accepted'})
              -[:SOURCE]->(source:ExtractedEntity)
        MATCH (c)-[:TARGET]->(target:ExtractedEntity)
        WHERE EXISTS { MATCH (c)-[:SUPPORTED_BY]->(:Evidence) }
        MERGE (sp:PublishedEntity {id:'published:' + source.id})
        SET sp.extracted_id=source.id, sp.title=coalesce(source.canonical_name, source.title), sp.aliases=coalesce(source.aliases, []), sp.entity_type=source.entity_type,
            sp.description=source.description, sp.graph_origin='module3',
            sp.status='Accepted', sp.updated_at=datetime()
        MERGE (tp:PublishedEntity {id:'published:' + target.id})
        SET tp.extracted_id=target.id, tp.title=coalesce(target.canonical_name, target.title), tp.aliases=coalesce(target.aliases, []), tp.entity_type=target.entity_type,
            tp.description=target.description, tp.graph_origin='module3',
            tp.status='Accepted', tp.updated_at=datetime()
        MERGE (sp)-[r:PUBLISHED_RELATION {candidate_id:c.id}]->(tp)
        SET r.description=c.description, r.relation_type=c.relation_type, r.weight=c.weight, r.status='Accepted',
            r.reviewer=c.reviewer, r.review_reason=c.review_reason,
            r.reviewed_at=c.reviewed_at, r.evidence_ids=c.evidence_ids,
            r.wiki_page_ids=c.wiki_page_ids, r.text_unit_ids=c.text_unit_ids,
            r.graph_origin='module3', r.updated_at=datetime()
        """
    ).consume()
    record = session.run(
        """
        MATCH (n:PublishedEntity {graph_origin:'module3'})
        OPTIONAL MATCH ()-[r:PUBLISHED_RELATION {graph_origin:'module3'}]->()
        RETURN count(DISTINCT n) AS entities, count(DISTINCT r) AS relationships
        """
    ).single()
    return {"entities": record["entities"], "relationships": record["relationships"]}


def _candidate_provenance(
    unit_ids: list[str],
    text_unit_to_pages: dict[str, set[str]],
    page_by_id: dict[str, dict],
    evidence_by_id: dict[str, dict],
) -> dict:
    page_ids = sorted(set().union(*(text_unit_to_pages.get(unit_id, set()) for unit_id in unit_ids))) if unit_ids else []
    evidence_ids: list[str] = []
    fingerprint_parts: list[dict] = []
    for page_id in page_ids:
        page = page_by_id[page_id]
        page_evidence = []
        for ref in page.get("evidenceRefs") or []:
            evidence_id = str(ref.get("evidenceId") or "")
            if evidence_id:
                evidence_ids.append(evidence_id)
                evidence = evidence_by_id.get(evidence_id, {})
                page_evidence.append({
                    "id": evidence_id,
                    "snapshot": ref.get("evidenceSnapshotId"),
                    "document_version": evidence.get("documentVersionId"),
                    "text": evidence.get("text"),
                })
        fingerprint_parts.append({
            "wiki_page_id": page_id,
            "wiki_page_version": page.get("wikiPageVersion", 1),
            "evidence": page_evidence,
        })
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_parts, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "wiki_page_ids": page_ids,
        "evidence_ids": sorted(set(evidence_ids)),
        "text_unit_ids": unit_ids,
        "provenance_fingerprint": fingerprint,
    }


def sync_neo4j(data: dict) -> dict:
    """Create reviewable extracted candidates and project accepted facts to Published."""
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise ValueError("Neo4j Python driver is not installed") from exc

    config = _neo4j_config(data)
    if not config["password"]:
        raise ValueError("Neo4j password is not configured")
    entity_rows = _read_parquet_rows("entities.parquet", limit=100000)
    relation_rows = _read_parquet_rows("relationships.parquet", limit=100000)
    text_unit_rows = _read_parquet_rows("text_units.parquet", limit=100000)
    document_rows = _read_parquet_rows("documents.parquet", limit=100000)
    for rows in (entity_rows, relation_rows, text_unit_rows, document_rows):
        if rows and "error" in rows[0]:
            raise ValueError(rows[0]["error"])
    if not entity_rows:
        raise ValueError("No GraphRAG entity output found; run indexing first")
    if not MANIFEST_PATH.exists():
        raise ValueError("No module 3 manifest found; prepare input first")

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    pages = manifest.get("input", {}).get("wikiPages", [])
    page_by_id = {
        str(page.get("wikiPageId") or f"wiki:{_safe_slug(str(page.get('title', 'page')))}"): page
        for page in pages
    }
    if not page_by_id:
        raise ValueError("No WikiPage input found")
    snapshot_key = json.dumps(manifest.get("input", {}), ensure_ascii=False, sort_keys=True)
    snapshot_id = _current_graph_snapshot_id(manifest)
    profile = _active_graph_profile()
    allowed_entity_types = set(profile["entityTypes"])
    allowed_relation_types = set(profile["relationTypes"])
    accepted_resolutions = {
        item["entityId"]: item
        for item in list_entity_resolutions()
        if item.get("status") == "Accepted"
    }
    snapshots, evidence_units = _load_evidence_units()
    snapshots_by_id = {str(item.get("evidenceSnapshotId")): item for item in snapshots}
    evidence_by_id = {str(item.get("evidenceId")): item for item in evidence_units}
    for page in page_by_id.values():
        page["evidenceRefs"] = _page_evidence_refs(page, evidence_by_id)

    documents: list[dict] = []
    evidences: list[dict] = []
    wikis: list[dict] = []
    wiki_evidence: list[dict] = []
    wiki_documents: list[dict] = []
    for page_id, page in page_by_id.items():
        wikis.append({
            "id": page_id, "title": str(page.get("title") or page_id),
            "page_version": page.get("wikiPageVersion", 1),
            "evidence_refs": json.dumps(page.get("evidenceRefs") or [], ensure_ascii=False),
            "snapshot": snapshot_id,
        })
        for ref in page.get("evidenceRefs") or []:
            evidence_id = str(ref.get("evidenceId") or "")
            evidence_snapshot = str(ref.get("evidenceSnapshotId") or "")
            evidence = evidence_by_id.get(evidence_id, {})
            if evidence_id:
                node_id = f"evidence:{evidence_id}"
                evidences.append({
                    "id": node_id, "evidence_id": evidence_id, "snapshot_id": evidence_snapshot,
                    "document_version_id": str(evidence.get("documentVersionId") or ""),
                    "heading": " / ".join(evidence.get("headingPath") or []),
                    "text": str(evidence.get("text") or ""), "snapshot": snapshot_id,
                })
                wiki_evidence.append({"wiki_id": page_id, "evidence_id": node_id})
            snapshot = snapshots_by_id.get(evidence_snapshot, {})
            doc = snapshot.get("document") or {}
            raw_id = str(doc.get("documentId") or "")
            if raw_id:
                node_id = f"document:{raw_id}"
                documents.append({
                    "id": node_id, "document_id": raw_id,
                    "document_version_id": str(doc.get("documentVersionId") or ""),
                    "title": str(doc.get("title") or raw_id),
                    "snapshot": snapshot_id,
                })
                wiki_documents.append({"wiki_id": page_id, "document_id": node_id})

    page_by_graph_file = {str(page.get("graphInputFile")): page_id for page_id, page in page_by_id.items()}
    document_to_pages: dict[str, set[str]] = {}
    for row in document_rows:
        page_id = page_by_graph_file.get(str(row.get("title") or ""))
        if page_id:
            document_to_pages.setdefault(str(row.get("id") or ""), set()).add(page_id)
            for text_unit_id in _as_list(row.get("text_unit_ids")):
                document_to_pages.setdefault(text_unit_id, set()).add(page_id)
    text_unit_to_pages: dict[str, set[str]] = {}
    text_units: list[dict] = []
    page_text_units: list[dict] = []
    for row in text_unit_rows:
        raw_id = str(row.get("id") or "")
        page_ids = document_to_pages.get(str(row.get("document_id") or ""), set()) | document_to_pages.get(raw_id, set())
        text_unit_to_pages[raw_id] = set(page_ids)
        text_units.append({"id": f"textunit:{raw_id}", "raw_id": raw_id, "text": str(row.get("text") or ""), "snapshot": snapshot_id})
        page_text_units.extend({"wiki_id": page_id, "text_unit_id": f"textunit:{raw_id}"} for page_id in page_ids)

    extracted_entities: list[dict] = []
    entity_candidates: list[dict] = []
    title_to_id: dict[str, str] = {}
    for row in entity_rows:
        title = str(row.get("title") or "unknown")
        entity_id = _stable_id("extracted-entity", title.casefold())
        title_to_id[title] = entity_id
        unit_ids = _as_list(row.get("text_unit_ids"))
        provenance = _candidate_provenance(unit_ids, text_unit_to_pages, page_by_id, evidence_by_id)
        entity_type = str(row.get("type") or "OTHER").upper()
        resolution = accepted_resolutions.get(entity_id, {})
        extracted_entities.append({
            "id": entity_id, "title": title, "entity_type": entity_type,
            "description": str(row.get("description") or ""), "frequency": row.get("frequency"),
            "degree": row.get("degree"), "snapshot": snapshot_id,
            "canonical_name": resolution.get("canonicalName"),
            "aliases": resolution.get("aliases", []),
            "resolution_status": resolution.get("status"),
        })
        entity_candidates.append({
            **provenance, "id": _stable_id("candidate-entity", entity_id),
            "entity_id": entity_id, "title": title, "entity_type": entity_type,
            "description": str(row.get("description") or ""), "snapshot": snapshot_id,
            "profile_id": profile["id"],
            "schema_status": "Valid" if entity_type in allowed_entity_types else "NeedsProfileReview",
        })

    relationship_candidates: list[dict] = []
    for row in relation_rows:
        source_title = str(row.get("source") or "")
        target_title = str(row.get("target") or "")
        source_id = title_to_id.get(source_title) or _stable_id("extracted-entity", source_title.casefold())
        target_id = title_to_id.get(target_title) or _stable_id("extracted-entity", target_title.casefold())
        unit_ids = _as_list(row.get("text_unit_ids"))
        provenance = _candidate_provenance(unit_ids, text_unit_to_pages, page_by_id, evidence_by_id)
        description = str(row.get("description") or "")
        relation_type = str(row.get("relation_type") or "RELATED").upper()
        relationship_candidates.append({
            **provenance,
            "id": _stable_id("candidate-relationship", source_id, target_id, description),
            "source_id": source_id, "target_id": target_id,
            "source_title": source_title, "target_title": target_title,
            "description": description, "weight": row.get("weight"),
            "combined_degree": row.get("combined_degree"), "snapshot": snapshot_id,
            "relation_type": relation_type,
            "profile_id": profile["id"],
            "schema_status": "Valid" if relation_type in allowed_relation_types else "NeedsProfileReview",
        })

    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        driver.verify_connectivity()
        with driver.session(database=config["database"]) as session:
            for statement in (
                "CREATE CONSTRAINT extracted_entity_id IF NOT EXISTS FOR (n:ExtractedEntity) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT entity_candidate_id IF NOT EXISTS FOR (n:EntityCandidate) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT relationship_candidate_id IF NOT EXISTS FOR (n:RelationshipCandidate) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT published_entity_id IF NOT EXISTS FOR (n:PublishedEntity) REQUIRE n.id IS UNIQUE",
                "CREATE CONSTRAINT text_unit_id IF NOT EXISTS FOR (n:TextUnit) REQUIRE n.id IS UNIQUE",
            ):
                session.run(statement).consume()
            # A full EUOS index represents one exact published snapshot. Remove only
            # Module 3's earlier projection so old candidates cannot leak into review.
            session.run("""
                MATCH (n {graph_origin:'module3'})
                WHERE n.graph_snapshot <> $snapshot
                DETACH DELETE n
            """, snapshot=snapshot_id).consume()
            session.run("UNWIND $rows AS row MERGE (n:WikiPage {id:row.id}) SET n.title=row.title, n.page_version=row.page_version, n.evidence_refs=row.evidence_refs, n.graph_origin='module3', n.graph_snapshot=row.snapshot, n.updated_at=datetime()", rows=wikis).consume()
            session.run("UNWIND $rows AS row MERGE (n:Evidence {id:row.id}) SET n.evidence_id=row.evidence_id, n.snapshot_id=row.snapshot_id, n.document_version_id=row.document_version_id, n.heading=row.heading, n.text=row.text, n.graph_origin='module3', n.graph_snapshot=row.snapshot, n.updated_at=datetime()", rows=evidences).consume()
            session.run("UNWIND $rows AS row MERGE (n:SourceDocument {id:row.id}) SET n.document_id=row.document_id, n.document_version_id=row.document_version_id, n.title=row.title, n.graph_origin='module3', n.graph_snapshot=row.snapshot, n.updated_at=datetime()", rows=documents).consume()
            session.run("UNWIND $rows AS row MERGE (n:TextUnit {id:row.id}) SET n.raw_id=row.raw_id, n.text=row.text, n.graph_origin='module3', n.graph_snapshot=row.snapshot, n.updated_at=datetime()", rows=text_units).consume()
            session.run("UNWIND $rows AS row MATCH (w:WikiPage {id:row.wiki_id}) MATCH (e:Evidence {id:row.evidence_id}) MERGE (w)-[:HAS_EVIDENCE]->(e)", rows=wiki_evidence).consume()
            session.run("UNWIND $rows AS row MATCH (w:WikiPage {id:row.wiki_id}) MATCH (d:SourceDocument {id:row.document_id}) MERGE (d)-[:HAS_WIKI]->(w)", rows=wiki_documents).consume()
            session.run("UNWIND $rows AS row MATCH (w:WikiPage {id:row.wiki_id}) MATCH (t:TextUnit {id:row.text_unit_id}) MERGE (w)-[:HAS_TEXT_UNIT]->(t)", rows=page_text_units).consume()
            session.run("UNWIND $rows AS row MERGE (e:ExtractedEntity {id:row.id}) SET e.title=row.title, e.entity_type=row.entity_type, e.description=row.description, e.frequency=row.frequency, e.degree=row.degree, e.canonical_name=row.canonical_name, e.aliases=row.aliases, e.resolution_status=row.resolution_status, e.layer='Extracted', e.graph_origin='module3', e.graph_snapshot=row.snapshot, e.updated_at=datetime()", rows=extracted_entities).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (c:EntityCandidate {id:row.id})
                ON CREATE SET c.status='Candidate', c.created_at=datetime()
                WITH c,row,c.status AS old_status,c.provenance_fingerprint AS old_fingerprint
                SET c.status=CASE WHEN old_fingerprint IS NOT NULL AND old_fingerprint <> row.provenance_fingerprint AND old_status IN ['Candidate','Accepted'] THEN 'Stale' ELSE old_status END,
                    c.stale_reason=CASE WHEN old_fingerprint IS NOT NULL AND old_fingerprint <> row.provenance_fingerprint THEN 'Wiki or Evidence version changed' ELSE c.stale_reason END,
                    c.title=row.title, c.entity_type=row.entity_type, c.description=row.description, c.profile_id=row.profile_id, c.schema_status=row.schema_status, c.wiki_page_ids=row.wiki_page_ids, c.evidence_ids=row.evidence_ids, c.text_unit_ids=row.text_unit_ids, c.provenance_fingerprint=row.provenance_fingerprint, c.graph_snapshot=row.snapshot, c.layer='Extracted', c.graph_origin='module3', c.updated_at=datetime()
                WITH c,row MATCH (e:ExtractedEntity {id:row.entity_id}) MERGE (c)-[:DESCRIBES]->(e)
            """, rows=entity_candidates).consume()
            session.run("""
                UNWIND $rows AS row
                MERGE (c:RelationshipCandidate {id:row.id})
                ON CREATE SET c.status='Candidate', c.created_at=datetime()
                WITH c,row,c.status AS old_status,c.provenance_fingerprint AS old_fingerprint
                SET c.status=CASE WHEN old_fingerprint IS NOT NULL AND old_fingerprint <> row.provenance_fingerprint AND old_status IN ['Candidate','Accepted'] THEN 'Stale' ELSE old_status END,
                    c.stale_reason=CASE WHEN old_fingerprint IS NOT NULL AND old_fingerprint <> row.provenance_fingerprint THEN 'Wiki or Evidence version changed' ELSE c.stale_reason END,
                    c.description=row.description, c.relation_type=row.relation_type, c.profile_id=row.profile_id, c.schema_status=row.schema_status, c.weight=row.weight, c.combined_degree=row.combined_degree, c.wiki_page_ids=row.wiki_page_ids, c.evidence_ids=row.evidence_ids, c.text_unit_ids=row.text_unit_ids, c.provenance_fingerprint=row.provenance_fingerprint, c.graph_snapshot=row.snapshot, c.layer='Extracted', c.graph_origin='module3', c.updated_at=datetime()
                WITH c,row MATCH (s:ExtractedEntity {id:row.source_id}) MATCH (t:ExtractedEntity {id:row.target_id}) MERGE (c)-[:SOURCE]->(s) MERGE (c)-[:TARGET]->(t)
            """, rows=relationship_candidates).consume()
            candidate_ids = [row["id"] for row in entity_candidates + relationship_candidates]
            relation_ids = [row["id"] for row in relationship_candidates]
            session.run("MATCH (c) WHERE (c:EntityCandidate OR c:RelationshipCandidate) AND c.graph_origin='module3' AND NOT c.id IN $ids AND c.status IN ['Candidate','Accepted'] SET c.status='Stale', c.stale_reason='No longer present in the latest GraphRAG output', c.updated_at=datetime()", ids=candidate_ids).consume()
            session.run("MATCH (c) WHERE (c:EntityCandidate OR c:RelationshipCandidate) AND c.id IN $ids OPTIONAL MATCH (c)-[r:FROM_WIKI|FROM_TEXT_UNIT|SUPPORTED_BY]->() DELETE r", ids=candidate_ids).consume()
            session.run("UNWIND $rows AS row MATCH (c {id:row.id}) UNWIND row.wiki_page_ids AS wiki_id MATCH (w:WikiPage {id:wiki_id}) MERGE (c)-[:FROM_WIKI]->(w)", rows=entity_candidates + relationship_candidates).consume()
            session.run("UNWIND $rows AS row MATCH (c {id:row.id}) UNWIND row.text_unit_ids AS text_id MATCH (t:TextUnit {id:'textunit:' + text_id}) MERGE (c)-[:FROM_TEXT_UNIT]->(t)", rows=entity_candidates + relationship_candidates).consume()
            session.run("UNWIND $rows AS row MATCH (c {id:row.id}) UNWIND row.evidence_ids AS evidence_id MATCH (e:Evidence {id:'evidence:' + evidence_id}) MERGE (c)-[:SUPPORTED_BY]->(e)", rows=entity_candidates + relationship_candidates).consume()
            session.run("MATCH ()-[r:EXTRACTED_RELATION {graph_origin:'module3'}]->() WHERE NOT r.candidate_id IN $ids DELETE r", ids=relation_ids).consume()
            session.run("""
                MATCH (c:RelationshipCandidate {graph_origin:'module3'})-[:SOURCE]->(s:ExtractedEntity)
                MATCH (c)-[:TARGET]->(t:ExtractedEntity)
                MERGE (s)-[r:EXTRACTED_RELATION {candidate_id:c.id}]->(t)
                SET r.description=c.description, r.relation_type=c.relation_type, r.weight=c.weight, r.status=c.status, r.evidence_ids=c.evidence_ids,
                    r.wiki_page_ids=c.wiki_page_ids, r.text_unit_ids=c.text_unit_ids, r.graph_origin='module3', r.updated_at=datetime()
            """).consume()
            published = _refresh_published_projection(session)
            snapshot_record = _record_snapshot(snapshot_id, {
                "entityCandidates": len(entity_candidates),
                "relationshipCandidates": len(relationship_candidates),
                "published": published,
                "manifestHash": hashlib.sha256(snapshot_key.encode("utf-8")).hexdigest(),
            })
        return {
            "ok": True, "uri": config["uri"], "database": config["database"], "mode": "governed-v3",
            "snapshot": snapshot_id, "entities": len(entity_candidates), "relationships": len(relationship_candidates),
            "text_units": len(text_units), "evidence": len(evidences), "published": published,
            "profile": profile["id"], "snapshotRecord": snapshot_record,
        }
    finally:
        driver.close()


def list_candidates(filters: dict) -> dict:
    config = _neo4j_config(filters)
    if not config["password"]:
        raise ValueError("Neo4j password is not configured")
    kind = str(filters.get("kind") or "all")
    status = str(filters.get("status") or "")
    try:
        limit = min(max(int(filters.get("limit") or 50), 1), 200)
        offset = max(int(filters.get("offset") or 0), 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit and offset must be integers") from exc
    labels = {"entity": "EntityCandidate", "relationship": "RelationshipCandidate"}
    if kind not in {"all", *labels}:
        raise ValueError("kind must be entity, relationship, or all")
    if status and status not in REVIEW_STATUSES:
        raise ValueError("Unknown review status")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else {}
    snapshot_id = _current_graph_snapshot_id(manifest)
    label_filter = f"c:{labels[kind]}" if kind in labels else "(c:EntityCandidate OR c:RelationshipCandidate)"
    status_filter = " AND c.status=$status" if status else ""
    count_query = f"""
        MATCH (c) WHERE {label_filter} AND c.graph_origin='module3' AND c.graph_snapshot=$snapshot{status_filter}
        RETURN count(c) AS total
    """
    query = f"""
        MATCH (c) WHERE {label_filter} AND c.graph_origin='module3' AND c.graph_snapshot=$snapshot{status_filter}
        WITH c
        ORDER BY CASE c.status WHEN 'Candidate' THEN 0 WHEN 'Stale' THEN 1 WHEN 'Accepted' THEN 2 ELSE 3 END, c.id DESC
        SKIP $offset
        LIMIT $limit
        OPTIONAL MATCH (c)-[:DESCRIBES]->(entity:ExtractedEntity)
        OPTIONAL MATCH (c)-[:SOURCE]->(source:ExtractedEntity)
        OPTIONAL MATCH (c)-[:TARGET]->(target:ExtractedEntity)
        OPTIONAL MATCH (c)-[:FROM_WIKI]->(wiki:WikiPage)
        OPTIONAL MATCH (c)-[:FROM_TEXT_UNIT]->(unit:TextUnit)
        OPTIONAL MATCH (c)-[:SUPPORTED_BY]->(evidence:Evidence)
        RETURN c.id AS candidate_id, CASE WHEN c:EntityCandidate THEN 'entity' ELSE 'relationship' END AS kind,
          c.status AS status, c.description AS description, c.entity_type AS entity_type, c.relation_type AS relation_type,
          c.profile_id AS profile_id, c.schema_status AS schema_status, c.weight AS weight,
          c.reviewer AS reviewer, c.review_reason AS review_reason, c.reviewed_at AS reviewed_at,
          c.stale_reason AS stale_reason, entity.title AS entity, source.title AS source, target.title AS target,
          [item IN collect(DISTINCT {{id:wiki.id,title:wiki.title,version:wiki.page_version}}) WHERE item.id IS NOT NULL] AS wikis,
          [item IN collect(DISTINCT {{id:unit.raw_id,text:unit.text}}) WHERE item.id IS NOT NULL] AS text_units,
          [item IN collect(DISTINCT {{id:evidence.evidence_id,heading:evidence.heading,text:evidence.text,document_version_id:evidence.document_version_id}}) WHERE item.id IS NOT NULL] AS evidence
        ORDER BY CASE status WHEN 'Candidate' THEN 0 WHEN 'Stale' THEN 1 WHEN 'Accepted' THEN 2 ELSE 3 END, candidate_id DESC
    """
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        with driver.session(database=config["database"]) as session:
            params = {
                "status": status,
                "snapshot": snapshot_id,
                "limit": limit,
                "offset": offset,
            }
            total = int(session.run(count_query, **params).single()["total"])
            items = [dict(row) for row in session.run(query, **params)]
            return {
                "items": items,
                "limit": limit,
                "offset": offset,
                "total": total,
                "hasMore": offset + len(items) < total,
            }
    finally:
        driver.close()


def review_candidate(data: dict) -> dict:
    candidate_id = str(data.get("candidateId") or "").strip()
    decision = str(data.get("status") or "").strip()
    reviewer = str(data.get("reviewer") or "").strip()
    reason = str(data.get("reason") or "").strip()
    if not candidate_id or decision not in {"Accepted", "Rejected"} or not reviewer:
        raise ValueError("candidateId, status (Accepted/Rejected), and reviewer are required")
    config = _neo4j_config(data)
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        with driver.session(database=config["database"]) as session:
            candidate = session.run("""
                MATCH (c {id:$id}) WHERE c:EntityCandidate OR c:RelationshipCandidate
                OPTIONAL MATCH (c)-[:SUPPORTED_BY]->(e:Evidence)
                RETURN c.id AS candidate_id, c.schema_status AS schema_status,
                       c:RelationshipCandidate AS is_relationship, count(e) AS evidence_count
            """, id=candidate_id).single()
            if candidate is None:
                raise ValueError("Candidate not found")
            if decision == "Accepted" and candidate["schema_status"] != "Valid":
                raise ValueError("Candidate 类型不在当前 Graph Profile 中，不能发布")
            if decision == "Accepted" and candidate["is_relationship"] and candidate["evidence_count"] == 0:
                raise ValueError("Relationships without Evidence cannot be accepted")
            record = session.run("""
                MATCH (c {id:$id}) WHERE c:EntityCandidate OR c:RelationshipCandidate
                SET c.status=$status, c.reviewer=$reviewer, c.review_reason=$reason, c.reviewed_at=$reviewed_at,
                    c.stale_reason=CASE WHEN $status='Accepted' THEN NULL ELSE c.stale_reason END, c.updated_at=datetime()
                RETURN c.id AS candidate_id, c.status AS status
            """, id=candidate_id, status=decision, reviewer=reviewer, reason=reason, reviewed_at=_now_iso()).single()
            session.run("""
                MATCH (c:RelationshipCandidate {id:$id})-[:SOURCE]->(s:ExtractedEntity)
                MATCH (c)-[:TARGET]->(t:ExtractedEntity)
                MATCH (s)-[r:EXTRACTED_RELATION {candidate_id:$id}]->(t)
                SET r.status=$status, r.updated_at=datetime()
            """, id=candidate_id, status=decision).consume()
            published = _refresh_published_projection(session)
            return {**dict(record), "evidence_count": candidate["evidence_count"], "published": published}
    finally:
        driver.close()


def hybrid_query(query: str, method: str, scope: str, max_hops: int = 3) -> dict:
    if method not in ALLOWED_METHODS:
        raise ValueError("Unsupported query method")
    if scope not in {"published", "all"}:
        raise ValueError("scope must be published or all")
    if not 1 <= max_hops <= 6:
        raise ValueError("maxHops must be between 1 and 6")
    code, answer = run_cli(["query", "--root", ".", "--method", method, query])
    result = {"ok": code == 0, "code": code, "answer": answer, "scope": scope, "max_hops": max_hops, "paths": [], "evidence": [], "candidate_states": []}
    if code != 0:
        return result
    config = _neo4j_config({})
    if not config["password"]:
        return result
    from neo4j import GraphDatabase
    # The answer can be long and mention many unrelated concepts. Anchor the
    # provenance path on entities named in the user's question first.
    query_text = query.casefold()
    terms = _query_terms(query)
    if scope == "published":
        cypher = """
            MATCH p=(s:PublishedEntity)-[:PUBLISHED_RELATION*1..6]->(t:PublishedEntity)
            WHERE length(p) <= $max_hops
              AND ANY(n IN nodes(p) WHERE n.extracted_id IN $anchor_ids)
            RETURN [n IN nodes(p) | {id:n.id,title:n.title,type:n.entity_type,status:n.status}] AS nodes,
                   [r IN relationships(p) | {candidate_id:r.candidate_id,description:r.description,status:r.status,evidence_ids:r.evidence_ids}] AS relationships
            LIMIT 12
        """
    else:
        cypher = """
            MATCH p=(s:ExtractedEntity)-[:EXTRACTED_RELATION*1..6]->(t:ExtractedEntity)
            WHERE length(p) <= $max_hops
              AND ALL(r IN relationships(p) WHERE r.status IN ['Candidate','Accepted'])
              AND ANY(n IN nodes(p) WHERE n.id IN $anchor_ids)
            RETURN [n IN nodes(p) | {id:n.id,title:n.title,type:n.entity_type,status:'Extracted'}] AS nodes,
                   [r IN relationships(p) | {candidate_id:r.candidate_id,description:r.description,status:r.status,evidence_ids:r.evidence_ids}] AS relationships
            LIMIT 12
        """
    driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
    try:
        with driver.session(database=config["database"]) as session:
            anchor_rows = list(session.run("""
                MATCH (e:ExtractedEntity {graph_origin:'module3'})
                WHERE any(term IN $terms WHERE size(term) >= 2 AND
                    (toLower(e.title) CONTAINS term OR term CONTAINS toLower(e.title)))
                RETURN e.id AS id, e.title AS title
                ORDER BY size(e.title) DESC
                LIMIT 12
            """, terms=terms))
            anchor_ids = [row["id"] for row in anchor_rows]
            # If no question entity is found, retain the old broad behavior as
            # a fallback instead of returning an empty graph context.
            if not anchor_ids:
                anchor_rows = list(session.run("""
                    MATCH (e:ExtractedEntity {graph_origin:'module3'})
                    WHERE toLower($answer) CONTAINS toLower(e.title)
                    RETURN e.id AS id, e.title AS title
                    LIMIT 12
                """, answer=answer))
                anchor_ids = [row["id"] for row in anchor_rows]
            paths = [dict(row) for row in session.run(cypher, anchor_ids=anchor_ids, max_hops=max_hops)] if anchor_ids else []
            candidate_ids = sorted({relation["candidate_id"] for path in paths for relation in path["relationships"] if relation.get("candidate_id")})
            evidence_rows = []
            states = []
            if candidate_ids:
                evidence_rows = [dict(row) for row in session.run("""
                    MATCH (c:RelationshipCandidate)-[:SUPPORTED_BY]->(e:Evidence)
                    WHERE c.id IN $ids
                    OPTIONAL MATCH (c)-[:FROM_WIKI]->(w:WikiPage)
                    OPTIONAL MATCH (d:SourceDocument)-[:HAS_WIKI]->(w)
                    OPTIONAL MATCH (c)-[:SOURCE]->(source:ExtractedEntity)
                    OPTIONAL MATCH (c)-[:TARGET]->(target:ExtractedEntity)
                    RETURN DISTINCT c.id AS candidate_id, c.status AS status, c.description AS relation_description,
                      source.title AS source_title, target.title AS target_title,
                      e.evidence_id AS evidence_id, e.heading AS heading, e.text AS text,
                      e.document_version_id AS document_version_id, w.title AS wiki_title, d.title AS document_title
                """, ids=candidate_ids)]
                evidence_rows = _rank_query_evidence(evidence_rows, query_text, terms)
                states = [dict(row) for row in session.run(
                    "MATCH (c) WHERE c.id IN $ids RETURN c.id AS candidate_id, c.status AS status, c.reviewer AS reviewer, c.reviewed_at AS reviewed_at",
                    ids=candidate_ids,
                )]
            result["paths"] = paths
            result["evidence"] = evidence_rows
            result["candidate_states"] = states
    finally:
        driver.close()
    return result


def _query_terms(query: str) -> list[str]:
    """Extract useful terms for local graph and evidence ranking."""
    raw_terms = re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9 _-]{1,}", query.casefold())
    ignored = {"包括什么", "有哪些", "是什么", "哪些业务系统", "系统", "架构", "企业", "中的"}
    terms = [term.strip() for term in raw_terms if term.strip() not in ignored]
    return list(dict.fromkeys(terms))


def _rank_query_evidence(rows: list[dict], query_text: str, terms: list[str]) -> list[dict]:
    """Keep a small, query-relevant evidence set instead of dumping every ref."""
    def score(row: dict) -> int:
        evidence_text = " ".join(str(row.get(key) or "") for key in ("heading", "text")).casefold()
        relation_text = " ".join(str(row.get(key) or "") for key in (
            "relation_description", "source_title", "target_title",
        )).casefold()
        # Relation text identifies the candidate; Evidence text decides which
        # source actually supports this particular question.
        matched = sum(1 for term in terms if term in evidence_text) * 10
        matched += sum(1 for term in terms if term in relation_text)
        entity_bonus = sum(3 for key in ("source_title", "target_title") if str(row.get(key) or "").casefold() in query_text)
        return matched * 10 + entity_bonus

    ranked = sorted(rows, key=lambda row: (-score(row), str(row.get("candidate_id")), str(row.get("evidence_id"))))
    kept: list[dict] = []
    per_candidate: dict[str, int] = {}
    for row in ranked:
        candidate_id = str(row.get("candidate_id") or "")
        if per_candidate.get(candidate_id, 0) >= 2:
            continue
        if score(row) <= 0 and kept:
            continue
        per_candidate[candidate_id] = per_candidate.get(candidate_id, 0) + 1
        kept.append(row)
        if len(kept) >= 8:
            break
    return kept


def list_graph_entities(limit: int = 200) -> list[dict]:
    config = _neo4j_config({})
    if config["password"]:
        try:
            from neo4j import GraphDatabase
            driver = GraphDatabase.driver(config["uri"], auth=(config["user"], config["password"]))
            try:
                with driver.session(database=config["database"]) as session:
                    return [dict(row) for row in session.run(
                        """
                        MATCH (e:ExtractedEntity {graph_origin:'module3'})
                        RETURN e.id AS id, e.title AS title, e.canonical_name AS canonical_name,
                               e.aliases AS aliases, e.entity_type AS type,
                               e.resolution_status AS resolution_status
                        ORDER BY e.title LIMIT $limit
                        """,
                        limit=limit,
                    )]
            finally:
                driver.close()
        except Exception:
            pass
    return [
        {
            "id": _stable_id("extracted-entity", str(row.get("title") or "unknown").casefold()),
            "title": str(row.get("title") or "unknown"),
            "type": str(row.get("type") or "OTHER").upper(),
            "canonical_name": None,
            "aliases": [],
            "resolution_status": None,
        }
        for row in _read_parquet_rows("entities.parquet", limit=limit)
        if "error" not in row
    ]


def request_graph_build(data: dict) -> dict:
    """Build the governed projection from the current local GraphRAG output."""
    build_id = _stable_id("graph-build", _now_iso(), os.urandom(8).hex())
    builds = _governance_items(BUILDS_PATH)
    build = {
        "id": build_id,
        "status": "Running",
        "requestedAt": _now_iso(),
        "profileId": _active_graph_profile()["id"],
        "source": "local-graphrag-output",
    }
    builds.append(build)
    _write_json(BUILDS_PATH, builds)
    try:
        result = sync_neo4j(data)
        build.update({
            "status": "Completed",
            "completedAt": _now_iso(),
            "snapshotId": result["snapshot"],
            "counts": {
                "entityCandidates": result["entities"],
                "relationshipCandidates": result["relationships"],
                "published": result["published"],
            },
        })
    except Exception as exc:
        build.update({"status": "Failed", "completedAt": _now_iso(), "error": str(exc)})
        _write_json(BUILDS_PATH, [item for item in builds if item.get("id") != build_id] + [build])
        raise
    _write_json(BUILDS_PATH, [item for item in builds if item.get("id") != build_id] + [build])
    return build


def module3_status() -> dict:
    _ensure_module3_dirs()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8")) if MANIFEST_PATH.exists() else None
    source = _active_euos_source()
    snapshots, evidence_units = _load_evidence_units()
    return {
        "evidenceFiles": sorted(path.name for path in EVIDENCE_DIR.glob("*.json")),
        "wikiFiles": sorted(path.name for path in WIKI_DIR.iterdir() if path.suffix.lower() in {".json", ".md"}),
        "prepared": bool(manifest),
        "manifest": manifest,
        "graphInputFiles": sorted(path.name for path in GRAPH_INPUT_DIR.glob("*.md")),
        "activeSource": source,
        "counts": {
            "wikiPages": len([path for path in WIKI_DIR.iterdir() if path.suffix.lower() in {".json", ".md"}]),
            "evidenceSnapshots": len(snapshots),
            "evidenceUnits": len(evidence_units),
        },
    }


class Handler(BaseHTTPRequestHandler):
    """Serve the UI and a few safe local GraphRAG operations."""

    def _send(self, status: int, body: str, content_type: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        if content_type == "application/json":
            content_type = "application/json; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if content_type.startswith(("text/html", "application/javascript")):
            self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send(200, (STATIC / "index.html").read_text(encoding="utf-8"), "text/html; charset=utf-8")
            return
        if path == "/governance.js":
            self._send(200, (STATIC / "governance.js").read_text(encoding="utf-8"), "application/javascript; charset=utf-8")
            return
        if path == "/api/status":
            output_dir = ROOT / "output"
            files = sorted(p.name for p in output_dir.glob("*.parquet")) if output_dir.exists() else []
            self._send(200, json.dumps({"root": str(ROOT), "indexed": bool(files), "files": files}), "application/json")
            return
        if path == "/api/module3/status":
            self._send(200, json.dumps(module3_status(), ensure_ascii=False), "application/json")
            return
        if path == "/api/module3/euos/config":
            self._send(200, json.dumps(euos_connection_status(), ensure_ascii=False), "application/json")
            return
        if path == "/api/module3/results":
            payload = {"manifest": module3_status().get("manifest")}
            for name in ("entities.parquet", "relationships.parquet", "communities.parquet", "community_reports.parquet"):
                payload[name.removesuffix(".parquet")] = _read_parquet_rows(name)
            self._send(200, json.dumps(payload, ensure_ascii=False), "application/json")
            return
        if path == "/api/neo4j/config":
            self._send(200, json.dumps({
                "uri": os.environ.get("NEO4J_URI") or NEO4J_URI_DEFAULT,
                "user": os.environ.get("NEO4J_USERNAME") or NEO4J_USER_DEFAULT,
                "database": os.environ.get("NEO4J_DATABASE") or NEO4J_DATABASE_DEFAULT,
            }, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph-profiles":
            self._send(200, json.dumps({"ok": True, "items": list_graph_profiles()}, ensure_ascii=False), "application/json")
            return
        if path.startswith("/api/v1/graph-profiles/"):
            profile_id = unquote(path.rsplit("/", 1)[-1])
            profile = next((item for item in list_graph_profiles() if item.get("id") == profile_id), None)
            if profile is None:
                self._send(404, json.dumps({"ok": False, "error": "Graph Profile not found"}, ensure_ascii=False), "application/json")
            else:
                self._send(200, json.dumps({"ok": True, "item": profile}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/entities":
            raw_limit = parse_qs(parsed.query).get("limit", ["200"])[0]
            try:
                limit = min(max(int(raw_limit), 1), 1000)
            except ValueError:
                limit = 200
            self._send(200, json.dumps({"ok": True, "items": list_graph_entities(limit)}, ensure_ascii=False), "application/json")
            return
        if path.startswith("/api/v1/graph/entities/"):
            entity_id = unquote(path.rsplit("/", 1)[-1])
            entity = next((item for item in list_graph_entities(1000) if item.get("id") == entity_id), None)
            if entity is None:
                self._send(404, json.dumps({"ok": False, "error": "Graph entity not found"}, ensure_ascii=False), "application/json")
            else:
                self._send(200, json.dumps({"ok": True, "item": entity}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/entity-resolutions":
            self._send(200, json.dumps({"ok": True, "items": list_entity_resolutions()}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/object-provider":
            self._send(200, json.dumps({"ok": True, "items": list_object_catalog()}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/object-mappings":
            self._send(200, json.dumps({"ok": True, "items": list_object_mappings()}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/snapshots":
            self._send(200, json.dumps({"ok": True, "items": list_graph_snapshots()}, ensure_ascii=False), "application/json")
            return
        if path.startswith("/api/v1/graph-snapshots/"):
            snapshot_id = unquote(path.rsplit("/", 1)[-1])
            snapshot = next((item for item in list_graph_snapshots() if item.get("id") == snapshot_id), None)
            if snapshot is None:
                self._send(404, json.dumps({"ok": False, "error": "Graph Snapshot not found"}, ensure_ascii=False), "application/json")
            else:
                self._send(200, json.dumps({"ok": True, "item": snapshot}, ensure_ascii=False), "application/json")
            return
        if path == "/api/v1/graph/quality-report":
            self._send(200, json.dumps({"ok": True, "report": graph_quality_report()}, ensure_ascii=False), "application/json")
            return
        if path in {"/api/candidates", "/api/v1/graph-candidates"}:
            query_parts = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}
            try:
                result = list_candidates(query_parts)
            except Exception as exc:  # noqa: BLE001
                self._send(
                    503,
                    json.dumps(
                        {"ok": False, "error": f"读取候选失败：{exc}"},
                        ensure_ascii=False,
                    ),
                    "application/json",
                )
                return
            self._send(200, json.dumps({"ok": True, **result}, ensure_ascii=False), "application/json")
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length) or "{}")
            if path == "/api/upload":
                name = Path(str(data.get("name", ""))).name
                encoded = str(data.get("content", ""))
                allowed = {".txt", ".md", ".csv", ".json", ".jsonl"}
                if not name or Path(name).suffix.lower() not in allowed:
                    raise ValueError("仅支持 .txt、.md、.csv、.json、.jsonl 文件")
                if not encoded:
                    raise ValueError("文件内容为空")
                payload = base64.b64decode(encoded, validate=True)
                input_dir = ROOT / "input"
                input_dir.mkdir(exist_ok=True)
                source = Path(name)
                saved_name = source.name
                (input_dir / saved_name).write_bytes(payload)
                self._send(200, json.dumps({"ok": True, "name": saved_name, "size": len(payload)}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/upload":
                kind = str(data.get("kind", "")).lower()
                name = Path(str(data.get("name", ""))).name
                encoded = str(data.get("content", ""))
                allowed = {"evidence": {".json"}, "wiki": {".json", ".md"}}
                if kind not in allowed or Path(name).suffix.lower() not in allowed[kind]:
                    raise ValueError("模块1只支持 EvidenceSnapshot JSON；模块2支持 WikiPage JSON 或 Markdown")
                if not encoded:
                    raise ValueError("文件内容为空")
                payload = base64.b64decode(encoded, validate=True)
                _ensure_module3_dirs()
                target_dir = EVIDENCE_DIR if kind == "evidence" else WIKI_DIR
                (target_dir / name).write_bytes(payload)
                self._send(200, json.dumps({"ok": True, "kind": kind, "name": name, "size": len(payload)}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/load_examples":
                _ensure_module3_dirs()
                contract_roots = [
                    Path(r"D:\euos-service-py"),
                    Path(r"D:\ecos-service-py"),
                    Path(r"D:\esos-service-py"),
                ]
                contract_root = next((path for path in contract_roots if path.exists()), None)
                if contract_root is None:
                    raise ValueError("找不到 euos-service-py / ecos-service-py 的契约样例目录")
                evidence_example = contract_root / "contracts" / "examples" / "evidence" / "perkins-maintenance-snapshot.json"
                wiki_example = contract_root / "contracts" / "examples" / "wiki" / "procedure-page.json"
                if not evidence_example.exists() or not wiki_example.exists():
                    raise ValueError(f"契约样例不完整：{contract_root}")
                shutil.copy2(evidence_example, TEST_CASES_ROOT / "module1" / "evidence-snapshot.json")
                shutil.copy2(wiki_example, TEST_CASES_ROOT / "module2" / "wiki-page.json")
                shutil.copy2(evidence_example, EVIDENCE_DIR / "module1-evidence-snapshot.json")
                shutil.copy2(wiki_example, WIKI_DIR / "module2-wiki-page.json")
                self._send(200, json.dumps({"ok": True, "message": "已加载模块1/模块2真实契约样例"}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/prepare":
                manifest = prepare_module3_input()
                self._send(200, json.dumps({"ok": True, "manifest": manifest}, ensure_ascii=False), "application/json")
                return
            if path == "/api/module3/euos/sync":
                result = sync_from_euos(data)
                self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
                return
            if path == "/api/v1/graph-profiles":
                profile = save_graph_profile(data)
                self._send(200, json.dumps({"ok": True, "item": profile}, ensure_ascii=False), "application/json")
                return
            if path == "/api/v1/graph/entity-resolutions":
                resolution = save_entity_resolution(data)
                self._send(200, json.dumps({"ok": True, "item": resolution}, ensure_ascii=False), "application/json")
                return
            if path == "/api/v1/graph/object-mappings":
                mapping = save_object_mapping(data)
                self._send(200, json.dumps({"ok": True, "item": mapping}, ensure_ascii=False), "application/json")
                return
            if path == "/api/v1/graph-builds":
                build = request_graph_build(data)
                self._send(200, json.dumps({"ok": True, "item": build}, ensure_ascii=False), "application/json")
                return
            if path == "/api/neo4j/sync":
                result = sync_neo4j(data)
                self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
                return
            if path == "/api/candidates/review" or (
                path.startswith("/api/v1/graph-candidates/") and path.endswith("/review")
            ):
                if path.startswith("/api/v1/graph-candidates/"):
                    data["candidateId"] = path.removeprefix("/api/v1/graph-candidates/").removesuffix("/review").strip("/")
                result = review_candidate(data)
                self._send(200, json.dumps({"ok": True, "candidate": result}, ensure_ascii=False), "application/json")
                return
            if path in {"/api/hybrid-query", "/api/v1/graph/query", "/api/v1/graph/paths"}:
                query = str(data.get("query", "")).strip()
                if not query:
                    raise ValueError("Question is required")
                try:
                    max_hops = int(data.get("maxHops", 3))
                except (TypeError, ValueError) as exc:
                    raise ValueError("maxHops must be an integer") from exc
                result = hybrid_query(query, str(data.get("method") or "global"), str(data.get("scope") or "published"), max_hops)
                if path == "/api/v1/graph/paths":
                    result.pop("answer", None)
                self._send(200, json.dumps(result, ensure_ascii=False), "application/json")
                return
            if path == "/api/query":
                query = str(data.get("query", "")).strip()
                method = str(data.get("method", "global"))
                if not query:
                    raise ValueError("请输入问题")
                if method not in ALLOWED_METHODS:
                    raise ValueError("不支持的检索方式")
                # Put options before the free-form query. This avoids Windows command
                # line parsing treating parts of a long query as extra arguments.
                # The CLI already runs with ROOT as its working directory.
                # Passing an absolute Windows path here is misparsed by the
                # installed GraphRAG CLI, so keep the root path relative.
                code, output = run_cli(["query", "--root", ".", "--method", method, query])
                evidence = build_query_evidence(query, output) if code == 0 else None
                response = {"ok": code == 0, "code": code, "output": output}
                if evidence is not None:
                    response["evidence"] = evidence
                self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
                return
            elif path == "/api/index":
                if not INDEX_LOCK.acquire(blocking=False):
                    response = {
                        "ok": False,
                        "code": None,
                        "output": (
                            "索引模式：本次全量索引（仅当前 EUOS 快照）\n"
                            "GraphRAG 索引未启动。\n"
                            "失败原因：已有索引任务正在运行，请等待当前任务结束，勿重复点击。"
                        ),
                        "error": "已有索引任务正在运行",
                        "neo4j": None,
                    }
                    self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
                    return
                try:
                    healthy, health_message = embedding_service_status()
                    if not healthy:
                        output = (
                            "索引模式：本次全量索引（仅当前 EUOS 快照）\n"
                            "GraphRAG 索引未启动。\n"
                            f"失败原因：{health_message}\n"
                            "请先运行 embedding_service\\start_embedding_service.ps1，"
                            "确认 http://127.0.0.1:8001/health 可访问后重试。"
                        )
                        response = {"ok": False, "code": None, "output": output, "error": health_message, "neo4j": None}
                        self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
                        return

                    code, raw_output = run_cli(["index", "--root", ".", "--verbose"], timeout=7200)
                    log_path = ROOT / "logs" / "module3-index.log"
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text(raw_output, encoding="utf-8")
                    pipeline_failure = _index_failure_reason(raw_output)
                    complete_outputs, output_files = _has_complete_index_outputs()
                    confirmed = code == 0 and pipeline_failure is None and complete_outputs
                    output = "索引模式：本次全量索引（仅当前 EUOS 快照）\n" + _compact_index_output(raw_output)
                    if not confirmed and pipeline_failure is None:
                        output += (
                            "\n失败原因：索引进程没有生成完整产物。"
                            " 需要 entities.parquet、relationships.parquet、text_units.parquet；"
                            f"当前产物：{', '.join(output_files) if output_files else '无'}。"
                        )
                    neo4j_result = None
                    if confirmed:
                        try:
                            neo4j_result = sync_neo4j({})
                            output += "\n\nNeo4j 自动同步完成：" + json.dumps(neo4j_result, ensure_ascii=False)
                        except Exception as exc:  # noqa: BLE001
                            neo4j_result = {"ok": False, "error": str(exc)}
                            output += "\n\nNeo4j 自动同步失败：" + str(exc)
                    code = 0 if confirmed else (code or 1)
                finally:
                    INDEX_LOCK.release()
            else:
                self._send(404, "Not found", "text/plain; charset=utf-8")
                return
            response = {"ok": code == 0, "code": code, "output": output}
            if path == "/api/index":
                response["neo4j"] = neo4j_result
            self._send(200, json.dumps(response, ensure_ascii=False), "application/json")
        except Exception as exc:  # noqa: BLE001
            self._send(400, json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), "application/json")


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"GraphRAG demo: http://127.0.0.1:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
