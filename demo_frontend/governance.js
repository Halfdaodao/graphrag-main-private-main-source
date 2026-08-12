(() => {
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const data = await response.json();
    if (!data.ok) throw new Error(data.error || "请求失败");
    return data;
  };
  const post = (url, body) => request(url, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });

  const style = document.createElement("style");
  style.textContent = `
    #governancePanel .governance-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
    #governancePanel .governance-box{border:1px solid var(--line);border-radius:5px;padding:12px}
    #governancePanel .governance-box h3{margin:0 0 8px}
    #governancePanel .governance-list{max-height:180px;overflow:auto;border-top:1px solid var(--line);margin-top:8px}
    #governancePanel .governance-item{padding:7px 0;border-bottom:1px solid var(--line);font-size:12px}
    #governancePanel .governance-item strong{display:block}
    #governancePanel .governance-actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
    #governancePanel .governance-actions button{padding:6px 9px}
    #governancePanel .governance-report{white-space:pre-wrap;background:#f8fafb;border:1px solid var(--line);padding:8px;font:12px/1.5 monospace}
    @media(max-width:1050px){#governancePanel .governance-grid{grid-template-columns:1fr}}
  `;
  document.head.appendChild(style);

  const panel = document.createElement("section");
  panel.id = "governancePanel";
  panel.innerHTML = `
    <div class="governed-row">
      <div><h2>图谱治理与本地 Mock</h2><div class="hint">正式接入上游前，用本地 Profile、实体消歧和业务对象验证模块 3 契约。</div></div>
      <div class="actions"><button id="refreshGovernance" class="secondary">刷新治理数据</button><button id="runGraphBuild">构建治理快照</button></div>
    </div>
    <div class="governance-grid">
      <div class="governance-box">
        <h3>Graph Profile</h3>
        <label for="profileName">Profile 名称</label><input id="profileName" value="设备维护知识图谱">
        <label for="profileEntities">实体类型（逗号分隔）</label><input id="profileEntities" value="COMPONENT,CONCEPT,DOCUMENT,EQUIPMENT,EVENT,FAILURE,LOCATION,MATERIAL,ORGANIZATION,OTHER,PERSON,PROCEDURE,PRODUCT,SAFETY_CONDITION,SUPPLIER">
        <label for="profileRelations">关系类型（逗号分隔）</label><input id="profileRelations" value="RELATED">
        <div class="governance-actions"><button id="saveProfile">保存并启用 Profile</button></div>
        <div id="profileList" class="governance-list"></div>
      </div>
      <div class="governance-box">
        <h3>实体消歧 / 别名审核</h3>
        <label for="resolutionEntity">实体</label><select id="resolutionEntity"></select>
        <label for="resolutionCanonical">Canonical Name</label><input id="resolutionCanonical" placeholder="标准名称">
        <label for="resolutionAliases">别名（逗号分隔）</label><input id="resolutionAliases" placeholder="别名、缩写、旧名称">
        <label for="resolutionReviewer">审核人</label><input id="resolutionReviewer" value="local-reviewer">
        <div class="governance-actions"><button id="saveResolution">接受消歧</button></div>
        <div id="resolutionList" class="governance-list"></div>
      </div>
      <div class="governance-box">
        <h3>Object Provider / 业务对象映射</h3>
        <label for="mappingEntity">知识实体</label><select id="mappingEntity"></select>
        <label for="mappingObject">业务对象</label><select id="mappingObject"></select>
        <label for="mappingReviewer">审核人</label><input id="mappingReviewer" value="local-reviewer">
        <div class="governance-actions"><button id="saveMapping">保存映射候选</button><button id="acceptMapping" class="secondary">接受映射</button></div>
        <div id="mappingList" class="governance-list"></div>
      </div>
      <div class="governance-box">
        <h3>快照与质量报告</h3>
        <div id="snapshotList" class="governance-list"></div>
        <div id="qualityReport" class="governance-report" style="margin-top:8px">尚未读取报告。</div>
      </div>
    </div>`;
  const modalContent = document.getElementById("governanceModalContent");
  if (!modalContent) return;
  modalContent.appendChild(panel);

  const $ = (id) => document.getElementById(id);
  const entityOptions = (entities) => entities.map((item) =>
    `<option value="${esc(item.id)}">${esc(item.canonical_name || item.title)} · ${esc(item.type || "")}</option>`).join("");

  async function refresh() {
    const [profiles, entities, objects, resolutions, mappings, snapshots, quality] = await Promise.all([
      request("/api/v1/graph-profiles"), request("/api/v1/graph/entities"),
      request("/api/v1/graph/object-provider"), request("/api/v1/graph/entity-resolutions"),
      request("/api/v1/graph/object-mappings"), request("/api/v1/graph/snapshots"),
      request("/api/v1/graph/quality-report"),
    ]);
    $("profileList").innerHTML = profiles.items.map((p) =>
      `<div class="governance-item"><strong>${esc(p.name)} ${p.active ? "（启用）" : ""}</strong>${esc(p.id)} · v${esc(p.version)}<br>实体 ${p.entityTypes.length} 类，关系 ${p.relationTypes.length} 类</div>`).join("") || "暂无 Profile";
    $("resolutionEntity").innerHTML = entityOptions(entities.items);
    $("mappingEntity").innerHTML = entityOptions(entities.items);
    $("mappingObject").innerHTML = objects.items.map((o) =>
      `<option value="${esc(o.id)}">${esc(o.name)} · ${esc(o.type)}</option>`).join("");
    $("resolutionList").innerHTML = resolutions.items.map((r) =>
      `<div class="governance-item"><strong>${esc(r.canonicalName)}</strong>${esc(r.entityId)} · 别名：${esc((r.aliases || []).join("、"))}<br>${esc(r.status)} · ${esc(r.reviewer)}</div>`).join("") || "暂无消歧记录";
    $("mappingList").innerHTML = mappings.items.map((m) =>
      `<div class="governance-item"><strong>${esc(m.objectName)}</strong>${esc(m.entityId)} → ${esc(m.objectId)}<br>${esc(m.status)} · ${esc(m.reviewer)}</div>`).join("") || "暂无对象映射";
    $("snapshotList").innerHTML = snapshots.items.slice(0, 8).map((s) =>
      `<div class="governance-item"><strong>${esc(s.id)}</strong>${esc(s.status)} · ${esc(s.createdAt)}<br>候选实体 ${esc(s.entityCandidates)}，候选关系 ${esc(s.relationshipCandidates)}</div>`).join("") || "暂无治理快照";
    $("qualityReport").textContent = JSON.stringify(quality.report, null, 2);
  }

  $("saveProfile").onclick = async () => {
    try {
      await post("/api/v1/graph-profiles", {
        name: $("profileName").value.trim(),
        entityTypes: $("profileEntities").value.split(","),
        relationTypes: $("profileRelations").value.split(","),
        active: true,
      });
      $("output").textContent = "Graph Profile 已保存并启用。";
      await refresh();
    } catch (error) { $("output").textContent = `保存 Profile 失败：${error}`; }
  };
  $("saveResolution").onclick = async () => {
    try {
      await post("/api/v1/graph/entity-resolutions", {
        entityId: $("resolutionEntity").value,
        canonicalName: $("resolutionCanonical").value.trim(),
        aliases: $("resolutionAliases").value.split(","),
        reviewer: $("resolutionReviewer").value.trim(),
        status: "Accepted",
      });
      $("output").textContent = "实体消歧已保存。下次同步或 Neo4j 在线时会投影 Canonical Name。";
      await refresh();
    } catch (error) { $("output").textContent = `保存消歧失败：${error}`; }
  };
  async function saveMapping(status) {
    try {
      await post("/api/v1/graph/object-mappings", {
        entityId: $("mappingEntity").value, objectId: $("mappingObject").value,
        reviewer: $("mappingReviewer").value.trim(), status,
      });
      $("output").textContent = status === "Accepted" ? "业务对象映射已接受。" : "业务对象映射候选已保存。";
      await refresh();
    } catch (error) { $("output").textContent = `保存对象映射失败：${error}`; }
  }
  $("saveMapping").onclick = () => saveMapping("Candidate");
  $("acceptMapping").onclick = () => saveMapping("Accepted");
  $("refreshGovernance").onclick = () => refresh().catch((error) => { $("output").textContent = `刷新治理数据失败：${error}`; });
  $("runGraphBuild").onclick = async () => {
    const button = $("runGraphBuild"); button.disabled = true; $("output").textContent = "正在构建治理快照…";
    try {
      const data = await post("/api/v1/graph-builds", {});
      $("output").textContent = `治理快照构建完成：${data.item.snapshotId || "未生成"}。`;
      await refresh();
    } catch (error) { $("output").textContent = `治理快照构建失败：${error}`; }
    finally { button.disabled = false; }
  };
  const governanceModal = $("governanceModal");
  $("openGovernance").onclick = () => {
    governanceModal.showModal();
    refresh().catch((error) => { $("output").textContent = `读取治理数据失败：${error}`; });
  };
  $("closeGovernance").onclick = () => governanceModal.close();
})();
