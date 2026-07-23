#!/usr/bin/env python3
"""Generate knowledge graph data from wiki and session memories."""
import json, os, re

nodes = []
edges = []
node_ids = {}

def add_node(id, label, type, color):
    if id not in node_ids:
        node_ids[id] = len(nodes)
        nodes.append({"id": id, "label": label, "type": type, "color": color})
    return node_ids[id]

# Add agents
agents = [
    ("apex", "Apex", "#2563eb"),
    ("rook", "Rook", "#7c3aed"),
    ("owl", "OWL", "#059669"),
    ("cxo", "CXO", "#94a3b8"),
]
for aid, alabel, acolor in agents:
    add_node(aid, alabel, "agent", acolor)

# Add projects
projects = [
    ("learnfast", "LearnFast LMS", "#f59e0b"),
    ("homeranker", "Home Ranker", "#ef4444"),
    ("helmward", "Helmward OS", "#2563eb"),
    ("apex_solutions", "Apex Solutions", "#64748b"),
]
for pid, plabel, pcolor in projects:
    add_node(pid, plabel, "project", pcolor)

# Add machines
machines = [
    ("ct201", "CT201\n192.168.1.147", "#0ea5e9"),
    ("windows", "Windows Box\n192.168.1.180", "#0ea5e9"),
    ("mac", "Mac M3 Pro\n192.168.1.128", "#0ea5e9"),
    ("macmini", "Mac Mini\n192.168.1.234", "#0ea5e9"),
    ("bazzite", "Bazzite\n192.168.1.139", "#0ea5e9"),
]
for mid, mlabel, mcolor in machines:
    add_node(mid, mlabel, "machine", mcolor)

# Agent -> machine edges
edges.append({"source": "apex", "target": "ct201", "label": "runs on"})
edges.append({"source": "rook", "target": "bazzite", "label": "runs on"})
edges.append({"source": "owl", "target": "macmini", "label": "runs on"})

# Agent -> model edges
add_node("qwythos", "Qwythos-9B", "model", "#8b5cf6")
add_node("qwen35", "Qwen3.5-9B", "model", "#8b5cf6")
add_node("gemma4", "Gemma 4 12B", "model", "#8b5cf6")
edges.append({"source": "apex", "target": "qwythos", "label": "uses model"})
edges.append({"source": "rook", "target": "qwen35", "label": "uses model"})

# Model -> machine edges
edges.append({"source": "qwythos", "target": "windows", "label": "served by"})
edges.append({"source": "qwen35", "target": "mac", "label": "served by"})
edges.append({"source": "gemma4", "target": "mac", "label": "available on"})

# Agent -> project edges
edges.append({"source": "apex", "target": "learnfast", "label": "builds"})
edges.append({"source": "apex", "target": "helmward", "label": "powers"})
edges.append({"source": "owl", "target": "homeranker", "label": "manages"})

# Read session memories and add as nodes
mem_file = "/root/.hermes/session_memories.txt"
if os.path.exists(mem_file):
    with open(mem_file) as f:
        facts = [x.strip() for x in f.read().split('\xa7') if x.strip()]
    for i, fact in enumerate(facts[:8]):  # limit to 8 facts
        fid = f"mem_{i}"
        label = fact[:40] + "..." if len(fact) > 40 else fact
        add_node(fid, label, "memory", "#f97316")
        # Connect to relevant nodes
        if "learnfast" in fact.lower():
            edges.append({"source": fid, "target": "learnfast", "label": "about"})
        if "apex" in fact.lower():
            edges.append({"source": fid, "target": "apex", "label": "about"})
        if "rook" in fact.lower():
            edges.append({"source": fid, "target": "rook", "label": "about"})
        if "helmward" in fact.lower():
            edges.append({"source": fid, "target": "helmward", "label": "about"})

# Read wiki files and add as nodes
wiki_dir = "/mnt/wiki/projects"
if os.path.exists(wiki_dir):
    for fname in os.listdir(wiki_dir):
        if fname.endswith(".md"):
            fid = f"wiki_{fname[:-3]}"
            add_node(fid, f"📄 {fname[:-3]}", "wiki", "#10b981")
            if "learnfast" in fname.lower():
                edges.append({"source": fid, "target": "learnfast", "label": "documents"})
            elif "helmward" in fname.lower():
                edges.append({"source": fid, "target": "helmward", "label": "documents"})

print(json.dumps({"nodes": nodes, "edges": edges}, indent=2))
