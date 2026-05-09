"""Thoth UI — knowledge-graph explorer panel.

Self-contained vis-network graph builder.  Can be called from any
NiceGUI parent context.
"""

from __future__ import annotations


def build_graph_panel() -> None:
    """Interactive knowledge graph explorer using vis-network.

    Imports ``knowledge_graph`` lazily — the panel simply shows an empty
    placeholder when no entities exist yet.
    """
    import json as _json

    from nicegui import ui

    import knowledge_graph as kg

    data = kg.graph_to_vis_json()
    stats = data["stats"]

    if stats["total_entities"] == 0:
        with ui.column().classes("w-full h-full items-center justify-center"):
            ui.icon("hub").classes("text-grey-6").style("font-size: 4rem; opacity: 0.4;")
            ui.label(
                "Your memory map will appear here as Thoth learns about you."
            ).classes("text-grey-6 text-center q-mt-md").style("max-width: 360px;")
        return

    nodes_json = _json.dumps(data["nodes"])
    edges_json = _json.dumps(data["edges"])
    center_id = _json.dumps(data["center"])
    type_colors = _json.dumps(kg._VIS_TYPE_COLORS)

    # ── Controls bar ─────────────────────────────────────────────────
    with ui.row().classes("w-full items-center gap-2 q-px-sm q-py-xs shrink-0").style(
        "border-bottom: 1px solid rgba(255,255,255,0.08);"
    ):
        ui.html(
            '<input id="graph-search" type="text" placeholder="Search entities…" '
            'style="background: #1e1e2e; border: 1px solid #444; border-radius: 6px; '
            'padding: 4px 10px; color: #eee; font-size: 0.85rem; width: 200px; '
            'outline: none;" />',
            sanitize=False,
        )
        ui.html(
            '<div id="graph-type-filters" style="display:flex; gap:4px; flex-wrap:wrap;"></div>',
            sanitize=False,
        )
        ui.html('<div style="flex-grow:1;"></div>', sanitize=False)
        # ── Source filter pills ──────────────────────────────────────
        ui.html(
            '<div id="graph-source-filters" style="display:flex; gap:4px; flex-wrap:wrap;">'
            '<button data-source="chat" title="From conversations (manual + extraction + system)" '
            'style="border:1px solid #42A5F5; background:none; border-radius:12px; '
            'padding:1px 8px; font-size:0.72rem; color:#42A5F5; cursor:pointer;">💬 chat</button>'
            '<button data-source="document" title="From uploaded documents" '
            'style="border:1px solid #AB47BC; background:none; border-radius:12px; '
            'padding:1px 8px; font-size:0.72rem; color:#AB47BC; cursor:pointer;">📄 documents</button>'
            '</div>',
            sanitize=False,
        )
        ui.html(
            '<label style="display:flex; align-items:center; gap:4px; font-size:0.8rem; color:#ccc; cursor:pointer;" title="Show/hide the User hub node">'
            '<input type="checkbox" id="graph-user-toggle" '
            'style="accent-color:#4FC3F7;" /> User hub</label>',
            sanitize=False,
        )
        ui.html(
            '<label style="display:flex; align-items:center; gap:4px; font-size:0.8rem; color:#ccc; cursor:pointer;" title="Hide entities with zero connections">'
            '<input type="checkbox" id="graph-orphan-toggle" '
            'style="accent-color:#FF8A65;" /> Hide orphans</label>',
            sanitize=False,
        )
        ui.html(
            f'<span id="graph-stats-label" style="font-size:0.75rem; color:#9E9E9E;">'
            f'{stats["shown_nodes"]} memories · {stats["shown_edges"]} connections'
            f'</span>',
            sanitize=False,
        )
        ui.html(
            '<button id="graph-fit-btn" title="Fit to view" '
            'style="background:none; border:1px solid #555; border-radius:4px; '
            'color:#ccc; padding:2px 8px; cursor:pointer; font-size:0.8rem;">'
            '⊞ Fit</button>',
            sanitize=False,
        )
        ui.html(
            '<button id="graph-full-toggle" style="'
            'font-size:0.75rem; color:#FFD54F; background:none; border:1px solid #FFD54F;'
            ' border-radius:4px; padding:2px 8px; cursor:pointer;"'
            '>Show All</button>',
            sanitize=False,
        )

        # ── "Run Dream Cycle" button ────────────────────────────────
        async def _run_dream_now():
            import dream_cycle as dc
            if not dc.is_enabled():
                ui.notify("Dream cycle is disabled in settings", type="warning")
                return
            btn_dream.disable()
            btn_dream.text = "Running…"
            try:
                result = await run.io_bound(dc.run_dream_cycle, lambda msg: None)
                summary = result.get("summary", "done")
                ui.notify(f"Dream cycle complete — {summary}", type="positive")
            except Exception as exc:
                ui.notify(f"Dream cycle failed: {exc}", type="negative")
            finally:
                btn_dream.text = "🌙 Dream"
                btn_dream.enable()

        from nicegui import run

        btn_dream = ui.button("🌙 Dream", on_click=_run_dream_now).props(
            "flat dense no-caps size=sm"
        ).style(
            "font-size:0.75rem; color:#CE93D8; border:1px solid #CE93D8; "
            "border-radius:4px; padding:2px 8px;"
        ).tooltip("Run dream cycle now (merge, enrich, infer)")

        # ── Hidden edit trigger (clicked from JS detail card) ────────
        async def _on_edit_click():
            eid = await ui.run_javascript(
                "window._thothGraph ? window._thothGraph._editEntityId : null"
            )
            if eid:
                from ui.entity_editor import open_entity_editor

                open_entity_editor(eid)

        _edit_btn = ui.button("edit", on_click=_on_edit_click)
        _edit_btn.props('id="graph-edit-trigger"')
        _edit_btn.style("display:none;")

    # ── vis-network canvas + overlay detail card ─────────────────────
    ui.html(
        '<div style="position:relative; width:100%; height:100%;">'
        '<div id="graph-container" style="width:100%; height:100%; background:#121212;"></div>'
        '<div id="graph-detail" style="display:none; position:absolute; bottom:8px; right:12px; '
        'padding:8px 12px; background:rgba(26,26,46,0.95); border:1px solid rgba(255,255,255,0.1); '
        'border-radius:8px; font-size:0.85rem; color:#ccc; max-height:140px; max-width:380px; '
        'overflow-y:auto; z-index:10; backdrop-filter:blur(6px); box-sizing:border-box;"></div>'
        '</div>',
        sanitize=False,
    ).style("flex:1; min-height:0; width:100%;")

    # ── vis-network JS logic ─────────────────────────────────────────
    _graph_js = (
        '(function() {'
        '  clearTimeout(window._thothGraphBootTimer || 0);'
        '  if (window._thothGraph) {'
        '    try { window._thothGraph.dispose && window._thothGraph.dispose(); } catch(e) {}'
        '    window._thothGraph = null;'
        '  }'
        '  var G = window._thothGraph = {'
        '    allNodes: ' + nodes_json + ','
        '    allEdges: ' + edges_json + ','
        '    centerId: ' + center_id + ','
        '    typeColors: ' + type_colors + ','
        '    network: null,'
        '    currentNodes: null,'
        '    currentEdges: null,'
        '    activeFilters: new Set(),'
        '    activeSourceFilters: new Set(),'
        '    showUserHub: true,'
        '    hideUnlinked: false,'
        '    isFullGraph: true,'
        '    searchDebounce: null,'
        '    physicsTimer: null'
        '  };'
        '  G.dispose = function() {'
        '    if (G.physicsTimer) { clearTimeout(G.physicsTimer); G.physicsTimer = null; }'
        '    if (G.network) { try { G.network.destroy(); } catch(e) {} G.network = null; }'
        '  };'
        # ── Helper: recency glow (days since updated_at → border width) ──
        '  G.recencyGlow = function(updatedAt) {'
        '    if (!updatedAt) return {bw: 1, bc: "#555"};'
        '    var ms = Date.now() - new Date(updatedAt).getTime();'
        '    var days = ms / 86400000;'
        '    if (days <= 7) return {bw: 3, bc: "#FFD54F"};'     # bright amber
        '    if (days <= 30) return {bw: 2, bc: "#FFA726"};'    # orange
        '    if (days <= 90) return {bw: 1, bc: "#8D6E63"};'    # dim brown
        '    return {bw: 1, bc: "#555"};'                        # stale
        '  };'
        # ── Helper: source-based border style ────────────────────────────
        '  G.isDocSource = function(src) {'
        '    return (src || "").indexOf("document") === 0;'
        '  };'
        '  G.sourceBorder = function(src) {'
        '    if (G.isDocSource(src)) return "false";'   # dashes=false means dashed in vis
        '    return undefined;'
        '  };'
        # ── Prepare nodes with recency + source styling ──────────────────
        '  G.styleNode = function(n) {'
        '    var glow = G.recencyGlow(n._updated_at);'
        '    var styled = Object.assign({}, n);'
        '    styled.borderWidth = glow.bw;'
        '    styled.color = {background: n.color, border: glow.bc,'
        '      highlight: {background: n.color, border: "#FFD54F"}};'
        '    if (G.isDocSource(n._source)) {'
        '      styled.shapeProperties = {borderDashes: [6,3]};'
        '    }'
        '    return styled;'
        '  };'
        '  G.allNodes = G.allNodes.map(G.styleNode);'
        '  G.currentNodes = G.allNodes;'
        '  G.currentEdges = G.allEdges;'
        # ── Find the User node ID ────────────────────────────────────────
        '  G.userNodeId = null;'
        '  for (var ui = 0; ui < G.allNodes.length; ui++) {'
        '    if ((G.allNodes[ui].label || "").toLowerCase() === "user") {'
        '      G.userNodeId = G.allNodes[ui].id; break;'
        '    }'
        '  }'
        # ── createNetwork ────────────────────────────────────────────────
        '  G.createNetwork = function(nodes, edges, focusId) {'
        '    var container = document.getElementById("graph-container");'
        '    if (!container) return;'
        '    var data = { nodes: new vis.DataSet(nodes), edges: new vis.DataSet(edges) };'
        '    var options = {'
        '      physics: {'
        '        solver: "forceAtlas2Based",'
        '        forceAtlas2Based: { gravitationalConstant: -40, centralGravity: 0.005,'
        '          springLength: 120, springConstant: 0.06, damping: 0.4 },'
        '        stabilization: { iterations: 150, fit: true }'
        '      },'
        '      nodes: { shape: "dot", borderWidth: 1, borderWidthSelected: 3, font: { size: 12 } },'
        '      edges: { smooth: { type: "continuous" }, width: 1, selectionWidth: 2,'
        '        font: { color: "transparent", strokeColor: "transparent", size: 11 } },'
        '      interaction: { hover: true, tooltipDelay: 200, hideEdgesOnDrag: true, multiselect: false }'
        '    };'
        '    G.dispose();'
        '    G.network = new vis.Network(container, data, options);'
        '    G.network.on("hoverEdge", function(p) {'
        '      data.edges.update({ id: p.edge, font: { color: "#ccc", strokeColor: "#222" } });'
        '    });'
        '    G.network.on("blurEdge", function(p) {'
        '      data.edges.update({ id: p.edge, font: { color: "transparent", strokeColor: "transparent" } });'
        '    });'
        '    G.network.once("stabilizationIterationsDone", function() {'
        '      if (window._thothGraph !== G || !G.network) return;'
        '      if (focusId) { G.network.focus(focusId, { scale: 1.0, animation: true }); }'
        '      else { G.network.fit({ animation: true }); }'
        '      if (G.physicsTimer) { clearTimeout(G.physicsTimer); }'
        '      G.physicsTimer = setTimeout(function() {'
        '        if (window._thothGraph !== G || !G.network) return;'
        '        try { G.network.setOptions({ physics: false }); } catch(e) {}'
        '        G.physicsTimer = null;'
        '      }, 5000);'
        '    });'
        # ── Click → detail card (enhanced with source + wiki link) ───────
        '    G.network.on("click", function(params) {'
        '      var detail = document.getElementById("graph-detail");'
        '      if (!detail) return;'
        '      if (params.nodes.length === 0) { detail.style.display = "none"; return; }'
        '      var nid = params.nodes[0];'
        '      var node = nodes.find(function(n) { return n.id === nid; });'
        '      if (!node) { detail.style.display = "none"; return; }'
        '      var rels = edges.filter(function(e) { return e.from === nid || e.to === nid; });'
        '      var relHtml = "";'
        '      if (rels.length > 0) {'
        '        var relItems = rels.slice(0, 10).map(function(e) {'
        '          var other = e.from === nid'
        '            ? nodes.find(function(n) { return n.id === e.to; })'
        '            : nodes.find(function(n) { return n.id === e.from; });'
        '          var dir = e.from === nid ? "\u2192" : "\u2190";'
        '          return "<span style=\\"color:#888;\\">" + dir + "</span> "'
        '            + "<b>" + (e.label || "related") + "</b> "'
        '            + (other ? other.label : "?");'
        '        }).join("<br>");'
        '        relHtml = "<div style=\\"margin-top:4px;\\">" + relItems'
        '          + (rels.length > 10 ? "<br><i>\u2026and " + (rels.length - 10) + " more</i>" : "")'
        '          + "</div>";'
        '      }'
        '      var aliases = node._aliases ? "<div style=\\"color:#999; font-size:0.8rem;\\">Aliases: " + node._aliases + "</div>" : "";'
        '      var tags = node._tags ? "<div style=\\"color:#999; font-size:0.8rem;\\">Tags: " + node._tags + "</div>" : "";'
        # Source label mapping
        '      var srcLabel = G.isDocSource(node._source) ? "📄 document" : "💬 chat";'
        # Time-ago helper
        '      var agoText = "";'
        '      if (node._updated_at) {'
        '        var agoDays = Math.floor((Date.now() - new Date(node._updated_at).getTime()) / 86400000);'
        '        agoText = agoDays === 0 ? "today" : agoDays === 1 ? "1 day ago" : agoDays + " days ago";'
        '      }'
        '      detail.innerHTML ='
        '        "<div style=\\"display:flex; align-items:center; gap:8px;\\">"'
        '        + "<span style=\\"background:" + (typeof node.color === "object" ? node.color.background : node.color) + "; width:10px; height:10px;"'
        '        + " border-radius:50%; display:inline-block;\\"></span>"'
        '        + "<b style=\\"color:#eee; font-size:1rem;\\">" + node.label + "</b>"'
        '        + "<span style=\\"color:#888; font-size:0.8rem;\\">(" + (node._type || "?") + ")</span>"'
        '        + "<span style=\\"color:#666; font-size:0.75rem; margin-left:auto;\\">"'
        '        + node._degree + " connections</span>"'
        '        + "</div>"'
        '        + (node._description ? "<div style=\\"margin-top:4px; color:#bbb;\\">" + node._description + "</div>" : "")'
        '        + aliases + tags + relHtml'
        '        + "<div style=\\"margin-top:4px; font-size:0.75rem; color:#777;\\">"'
        '        + "Source: " + srcLabel'
        '        + (agoText ? " \u00b7 Updated: " + agoText : "")'
        '        + " \\u00b7 <a href=\\"#\\" data-eid=\\"" + nid + "\\" onclick=\\"event.preventDefault(); var G=window._thothGraph; G._editEntityId=this.dataset.eid; document.getElementById(\\x27graph-edit-trigger\\x27).click();\\" style=\\"color:#90CAF9; text-decoration:none; cursor:pointer;\\">\\u270F\\uFE0F Edit</a>"'
        '        + "</div>";'
        '      detail.style.display = "block";'
        '    });'
        '    G.network.on("doubleClick", function(params) {'
        '      if (params.nodes.length === 0) return;'
        '      G.refocusOnNode(params.nodes[0]);'
        '    });'
        '    return G.network;'
        '  };'
        # ── refocusOnNode ────────────────────────────────────────────────
        '  G.refocusOnNode = function(nodeId) {'
        '    var hops = 2, visited = new Set([nodeId]), frontier = [nodeId];'
        '    for (var h = 0; h < hops; h++) {'
        '      var next = [];'
        '      for (var fi = 0; fi < frontier.length; fi++) {'
        '        var fid = frontier[fi];'
        '        for (var ei = 0; ei < G.allEdges.length; ei++) {'
        '          var e = G.allEdges[ei];'
        '          if (e.from === fid && !visited.has(e.to)) { visited.add(e.to); next.push(e.to); }'
        '          if (e.to === fid && !visited.has(e.from)) { visited.add(e.from); next.push(e.from); }'
        '        }'
        '      }'
        '      frontier = next;'
        '    }'
        '    var subNodes = G.allNodes.filter(function(n) { return visited.has(n.id); });'
        '    var subEdges = G.allEdges.filter(function(e) { return visited.has(e.from) && visited.has(e.to); });'
        '    G.currentNodes = subNodes; G.currentEdges = subEdges; G.isFullGraph = false;'
        '    G.updateStatsLabel(subNodes.length, subEdges.length);'
        '    G.createNetwork(subNodes, subEdges, nodeId);'
        '  };'
        # ── buildFilterPills ─────────────────────────────────────────────
        '  G.buildFilterPills = function() {'
        '    var container = document.getElementById("graph-type-filters");'
        '    if (!container) return;'
        '    var typeSet = new Set(G.allNodes.map(function(n) { return n._type; }));'
        '    var types = Array.from(typeSet).sort();'
        '    container.innerHTML = "";'
        '    for (var i = 0; i < types.length; i++) {'
        '      (function(t) {'
        '        var color = G.typeColors[t] || "#B0BEC5";'
        '        var pill = document.createElement("button");'
        '        pill.textContent = t; pill.dataset.type = t;'
        '        pill.style.cssText = "border:1px solid " + color + "; background:none;"'
        '          + " border-radius:12px; padding:1px 8px; font-size:0.72rem;"'
        '          + " color:" + color + "; cursor:pointer; transition:all 0.2s;";'
        '        pill.onclick = function() {'
        '          if (G.activeFilters.has(t)) {'
        '            G.activeFilters.delete(t); pill.style.background = "none"; pill.style.color = color;'
        '          } else {'
        '            G.activeFilters.add(t); pill.style.background = color; pill.style.color = "#121212";'
        '          }'
        '          G.applyFilters();'
        '        };'
        '        container.appendChild(pill);'
        '      })(types[i]);'
        '    }'
        '  };'
        # ── applyFilters (enhanced with source + user hub + unlinked) ────
        '  G.applyFilters = function() {'
        '    var searchVal = (document.getElementById("graph-search") || {}).value || "";'
        '    searchVal = searchVal.toLowerCase();'
        '    var hasTypeFilter = G.activeFilters.size > 0;'
        '    var hasSourceFilter = G.activeSourceFilters.size > 0;'
        '    var hasSearch = searchVal.length > 0;'
        '    var hasAny = hasTypeFilter || hasSourceFilter || hasSearch || !G.showUserHub || G.hideOrphans;'
        '    var ds = G.network.body.data.nodes;'
        '    var eds = G.network.body.data.edges;'
        '    if (!hasAny) {'
        '      var nBatch = [];'
        '      G.allNodes.forEach(function(n) {'
        '        if (ds.get(n.id) === null) return;'
        '        nBatch.push({id: n.id, opacity: 1.0, color: n.color, font: {color: "#ccc", size: 12}});'
        '      });'
        '      if (nBatch.length) ds.update(nBatch);'
        '      var eBatch = [];'
        '      G.allEdges.forEach(function(e) {'
        '        if (eds.get(e.id) === null) return;'
        '        eBatch.push({id: e.id, color: {opacity: 1.0}});'
        '      });'
        '      if (eBatch.length) eds.update(eBatch);'
        '      G.updateStatsLabel(G.allNodes.length, G.allEdges.length);'
        '      return;'
        '    }'
        # Build set of User hub edges (edges to/from User node)
        '    var userEdges = new Set();'
        '    if (!G.showUserHub && G.userNodeId) {'
        '      G.allEdges.forEach(function(e) {'
        '        if (e.from === G.userNodeId || e.to === G.userNodeId) userEdges.add(e.id);'
        '      });'
        '    }'
        # Count total connections per node (for orphan detection)
        '    var totalDeg = {};'
        '    if (G.hideOrphans) {'
        '      G.allNodes.forEach(function(n) { totalDeg[n.id] = 0; });'
        '      G.allEdges.forEach(function(e) {'
        '        totalDeg[e.from] = (totalDeg[e.from] || 0) + 1;'
        '        totalDeg[e.to] = (totalDeg[e.to] || 0) + 1;'
        '      });'
        '    }'
        '    var matchSet = new Set();'
        '    G.allNodes.forEach(function(n) {'
        # Type filter
        '      var typeOk = !hasTypeFilter || G.activeFilters.has(n._type);'
        # Source filter — "chat" matches non-document, "document" matches document:*
        '      var srcOk = !hasSourceFilter || (function() {'
        '        var s = n._source || "live";'
        '        var isDoc = G.isDocSource(s);'
        '        if (G.activeSourceFilters.has("document") && isDoc) return true;'
        '        if (G.activeSourceFilters.has("chat") && !isDoc) return true;'
        '        return false;'
        '      })();'
        # Search filter
        '      var searchOk = !hasSearch || n.label.toLowerCase().indexOf(searchVal) >= 0'
        '        || (n._description || "").toLowerCase().indexOf(searchVal) >= 0'
        '        || (n._aliases || "").toLowerCase().indexOf(searchVal) >= 0'
        '        || (n._tags || "").toLowerCase().indexOf(searchVal) >= 0;'
        # User hub filter: hide User node itself
        '      var userOk = G.showUserHub || n.id !== G.userNodeId;'
        # Orphan filter: hide nodes with 0 total connections
        '      var linkedOk = !G.hideOrphans || (totalDeg[n.id] || 0) > 0;'
        '      if (typeOk && srcOk && searchOk && userOk && linkedOk) matchSet.add(n.id);'
        '    });'
        '    var nBatch = [];'
        '    G.allNodes.forEach(function(n) {'
        '      if (ds.get(n.id) === null) return;'
        '      if (matchSet.has(n.id)) {'
        '        nBatch.push({id: n.id, opacity: 1.0, color: n.color, font: {color: "#ccc", size: 12}});'
        '      } else {'
        '        nBatch.push({id: n.id, opacity: 0.12, color: "#555", font: {color: "transparent", size: 0}});'
        '      }'
        '    });'
        '    if (nBatch.length) ds.update(nBatch);'
        '    var eBatch = [];'
        '    G.allEdges.forEach(function(e) {'
        '      if (eds.get(e.id) === null) return;'
        '      var both = matchSet.has(e.from) && matchSet.has(e.to);'
        '      var hiddenUserEdge = !G.showUserHub && userEdges.has(e.id);'
        '      eBatch.push({id: e.id, color: {opacity: (both && !hiddenUserEdge) ? 1.0 : 0.06}});'
        '    });'
        '    if (eBatch.length) eds.update(eBatch);'
        '    var visibleEdges = G.allEdges.filter(function(e) { return matchSet.has(e.from) && matchSet.has(e.to) && (G.showUserHub || !userEdges.has(e.id)); });'
        '    G.updateStatsLabel(matchSet.size, visibleEdges.length);'
        '  };'
        # ── updateStatsLabel ─────────────────────────────────────────────
        '  G.updateStatsLabel = function(nodeCount, edgeCount) {'
        '    var el = document.getElementById("graph-stats-label");'
        '    if (el) el.textContent = nodeCount + " memories \u00b7 " + edgeCount + " connections";'
        '  };'
        # ── wireControls (enhanced with source + user hub + unlinked) ────
        '  G.wireControls = function() {'
        '    G.buildFilterPills();'
        '    var searchInput = document.getElementById("graph-search");'
        '    if (searchInput) {'
        '      searchInput.oninput = function() {'
        '        clearTimeout(G.searchDebounce);'
        '        G.searchDebounce = setTimeout(function() { G.applyFilters(); }, 300);'
        '      };'
        '    }'
        # Source filter buttons
        '    document.querySelectorAll("#graph-source-filters button").forEach(function(btn) {'
        '      btn.onclick = function() {'
        '        var src = btn.dataset.source;'
        '        if (G.activeSourceFilters.has(src)) {'
        '          G.activeSourceFilters.delete(src);'
        '          btn.style.background = "none";'
        '          btn.style.color = btn.style.borderColor;'
        '        } else {'
        '          G.activeSourceFilters.add(src);'
        '          btn.style.background = btn.style.borderColor;'
        '          btn.style.color = "#121212";'
        '        }'
        '        G.applyFilters();'
        '      };'
        '    });'
        # User hub toggle
        '    var userToggle = document.getElementById("graph-user-toggle");'
        '    if (userToggle) {'
        '      userToggle.checked = G.showUserHub;'
        '      userToggle.onchange = function() {'
        '        G.showUserHub = userToggle.checked;'
        '        G.applyFilters();'
        '      };'
        '    }'
        # Hide orphans toggle
        '    var orphanToggle = document.getElementById("graph-orphan-toggle");'
        '    if (orphanToggle) {'
        '      orphanToggle.checked = G.hideOrphans;'
        '      orphanToggle.onchange = function() {'
        '        G.hideOrphans = orphanToggle.checked;'
        '        G.applyFilters();'
        '      };'
        '    }'
        '    var fitBtn = document.getElementById("graph-fit-btn");'
        '    if (fitBtn) {'
        '      fitBtn.onclick = function() { if (G.network) G.network.fit({ animation: true }); };'
        '    }'
        '    var fullToggle = document.getElementById("graph-full-toggle");'
        '    if (fullToggle) {'
        '      fullToggle.onclick = function() {'
        '        G.isFullGraph = true; G.currentNodes = G.allNodes; G.currentEdges = G.allEdges;'
        '        G.activeFilters.clear();'
        '        G.activeSourceFilters.clear();'
        '        var si = document.getElementById("graph-search"); if (si) si.value = "";'
        '        document.querySelectorAll("#graph-type-filters button").forEach(function(b) {'
        '          var c = G.typeColors[b.dataset.type] || "#B0BEC5";'
        '          b.style.background = "none"; b.style.color = c;'
        '        });'
        '        document.querySelectorAll("#graph-source-filters button").forEach(function(b) {'
        '          b.style.background = "none"; b.style.color = b.style.borderColor;'
        '        });'
        '        G.updateStatsLabel(G.allNodes.length, G.allEdges.length);'
        '        G.createNetwork(G.allNodes, G.allEdges, G.centerId);'
        '      };'
        '    }'
        '  };'
        # ── Boot ─────────────────────────────────────────────────────────
        '  function boot() {'
        '    if (!document.getElementById("graph-container")) {'
        '      window._thothGraphBootTimer = setTimeout(boot, 100);'
        '      return;'
        '    }'
        '    G.wireControls();'
        '    G.createNetwork(G.allNodes, G.allEdges, G.centerId);'
        '    G.applyFilters();'   # Apply default filters AFTER network exists
        '    window.thothGraphRedraw = function() {'
        '      if (!document.getElementById("graph-container")) return;'
        '      G.wireControls();'
        '      G.createNetwork(G.currentNodes, G.currentEdges, null);'
        '      G.applyFilters();'
        '    };'
        '  }'
        '  boot();'
        '})();'
    )
    ui.run_javascript(_graph_js)
