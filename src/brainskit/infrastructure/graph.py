from __future__ import annotations

import html
import json
from pathlib import PurePosixPath
from typing import Any

from brainskit.application.pages import parse_frontmatter
from brainskit.application.ports import VaultPort
from brainskit.domain.model import WIKI_LINK_RE, ValidationError


class MarkdownGraph:
    def build(self, vault: VaultPort) -> dict[str, Any]:
        nodes: dict[str, dict[str, Any]] = {}
        edges: set[tuple[str, str, str]] = set()
        records = vault.registry()
        for record in records.values():
            node_id = f"raw:{record.content_hash}"
            nodes[node_id] = {
                "id": node_id,
                "label": record.original_name,
                "kind": "raw",
                "path": record.path,
            }
        slug_nodes: dict[str, str] = {}
        page_content: dict[str, str] = {}
        for path in vault.wiki_pages():
            content = vault.read_text(path)
            metadata, _ = parse_frontmatter(content)
            slug = PurePosixPath(path).stem
            node_id = f"page:{path}"
            slug_nodes[slug] = node_id
            page_content[node_id] = content
            nodes[node_id] = {
                "id": node_id,
                "label": str(metadata.get("title", slug)),
                "kind": str(metadata.get("type", "wiki")),
                "path": path,
            }
            sources = metadata.get("sources", [])
            if isinstance(sources, list):
                for content_hash in sources:
                    raw_id = f"raw:{content_hash}"
                    if raw_id in nodes:
                        edges.add((node_id, raw_id, "sourced_from"))
        for node_id, content in page_content.items():
            for raw_link in WIKI_LINK_RE.findall(content):
                target = slug_nodes.get(PurePosixPath(raw_link.strip()).name)
                if target and target != node_id:
                    edges.add((node_id, target, "links_to"))
        return {
            "version": 1,
            "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
            "edges": [
                {"source": source, "target": target, "type": kind}
                for source, target, kind in sorted(edges)
            ],
        }

    def export(self, graph: dict[str, Any], target: str) -> str:
        if target == "json":
            return json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
        if target == "html":
            return _html_export(graph)
        if target == "graphml":
            return _graphml_export(graph)
        if target in {"cypher", "neo4j", "kuzu"}:
            return _cypher_export(graph)
        if target == "llms-txt":
            return _llms_export(graph)
        raise ValidationError("Unsupported graph export target", details={"target": target})


def _html_export(graph: dict[str, Any]) -> str:
    payload = json.dumps(graph, ensure_ascii=False).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>brainskit graph</title>
<style>
body{{margin:0;font:14px system-ui;background:#111827;color:#e5e7eb}}
header{{padding:16px 20px;border-bottom:1px solid #374151}}
#graph{{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px;padding:20px}}
.node{{padding:12px;border:1px solid #374151;border-radius:10px;background:#1f2937}}
.kind{{color:#93c5fd;font-size:12px}} input{{margin-left:16px;padding:8px;width:min(420px,55vw)}}
</style>
</head>
<body>
<header><strong>brainskit graph</strong><input id="q" placeholder="Filter nodes"></header>
<main id="graph"></main>
<script>
const data={payload}; const root=document.querySelector('#graph');
function render(q=''){{root.replaceChildren(...data.nodes.filter(n =>
(n.label+' '+n.kind+' '+n.path).toLowerCase().includes(q.toLowerCase())).map(n=>{{
const d=document.createElement('div');d.className='node';
const b=document.createElement('strong');b.textContent=n.label;
const k=document.createElement('div');k.className='kind';k.textContent=n.kind;
const p=document.createElement('div');p.textContent=n.path;d.append(b,k,p);return d}}))}}
document.querySelector('#q').addEventListener('input',e=>render(e.target.value));render();
</script>
</body></html>
"""


def _graphml_export(graph: dict[str, Any]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<key id="label" for="node" attr.name="label" attr.type="string"/>',
        '<graph id="brainskit" edgedefault="directed">',
    ]
    for node in graph["nodes"]:
        lines.append(
            f'<node id="{html.escape(node["id"], quote=True)}">'
            f'<data key="label">{html.escape(node["label"])}</data></node>'
        )
    for index, edge in enumerate(graph["edges"]):
        lines.append(
            f'<edge id="e{index}" source="{html.escape(edge["source"], quote=True)}" '
            f'target="{html.escape(edge["target"], quote=True)}"/>'
        )
    lines.extend(["</graph>", "</graphml>"])
    return "\n".join(lines) + "\n"


def _cypher_export(graph: dict[str, Any]) -> str:
    lines: list[str] = []
    for node in graph["nodes"]:
        properties = (
            "{id: "
            + _cypher_string(node["id"])
            + ", label: "
            + _cypher_string(node["label"])
            + ", kind: "
            + _cypher_string(node["kind"])
            + ", path: "
            + _cypher_string(node["path"])
            + "}"
        )
        lines.append(
            "MERGE (n:BrainkitNode {id: "
            + _cypher_string(node["id"])
            + "}) SET n += "
            + properties
            + ";"
        )
    for edge in graph["edges"]:
        relationship = (
            "SOURCED_FROM" if edge["type"] == "sourced_from" else "LINKS_TO"
        )
        lines.append(
            f"MATCH (a:BrainkitNode {{id: {_cypher_string(edge['source'])}}}), "
            f"(b:BrainkitNode {{id: {_cypher_string(edge['target'])}}}) "
            f"MERGE (a)-[:{relationship}]->(b);"
        )
    return "\n".join(lines) + "\n"


def _cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _llms_export(graph: dict[str, Any]) -> str:
    return "\n".join(
        f"- {node['label']} ({node['kind']}): {node['path']}"
        for node in graph["nodes"]
    ) + "\n"
