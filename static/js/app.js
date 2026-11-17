(() => {
  const main = document.getElementById("main");
  let view = "policies";
  let meta = { grace_disposition: "reject", grace_disposition_label: "整单拒绝销毁" };

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      const err = new Error(data.error || data.detail || "请求失败");
      err.data = data;
      throw err;
    }
    return data;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行" || state === "进行中"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁" || state === "已解除"
          ? "ok"
          : state === "已失败" || state === "已中止"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${esc(state)}</span>`;
  }

  function graceTag(item) {
    if (item.in_grace) {
      return `<span class="tag bad">宽限至 ${esc(item.grace_until)} · 剩 ${esc(item.days_left)} 天</span>`;
    }
    if (item.grace_until) {
      return `<span class="tag ok">宽限已过 ${esc(item.grace_until)}</span>`;
    }
    return `<span class="tag">无宽限</span>`;
  }

  function reasonList(reasons) {
    if (!reasons || !reasons.length) return `<span class="tag ok">可执行</span>`;
    return reasons.map((r) => `<div class="reason">${esc(r.text)}</div>`).join("");
  }

  function errBox(err) {
    const data = err.data || {};
    const reasons = (data.block_reasons || [])
      .map((r) => `<li>${esc(r.text)}</li>`)
      .join("");
    return `<div class="alert bad">
      <strong>${esc(err.message)}</strong>
      ${reasons ? `<ul>${reasons}</ul>` : ""}
    </div>`;
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td>${esc(p.domain_name)}</td>
              <td class="mono">${esc(p.domain_code)}</td>
              <td>${esc(p.keep_days)}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${esc(p.version)}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">宽限判定与裁决列表、分区详情同源：宽限未到期时销毁一律拦截。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>宽限</th><th>销毁判定</th><th>归档地址</th><th>操作</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${esc(p.domain_code)}</td>
              <td class="mono">${esc(p.biz_date)}</td>
              <td>${esc(p.row_count)}</td>
              <td>${tag(p.storage_state)}</td>
              <td>${p.frozen ? `<span class="tag bad">只读冻结${p.freeze_until ? ` 至 ${esc(p.freeze_until)}` : ""}</span> ` : ""}${graceTag(p.grace || p)}</td>
              <td>${reasonList(p.destroy_reasons)}</td>
              <td class="mono">${esc(p.archive_uri || "—")}</td>
              <td><button type="button" class="secondary" data-detail="${p.id}">详情</button></td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-detail]").forEach((btn) => {
      btn.onclick = () => showPartitionDetail(btn.dataset.detail);
    });
  }

  async function showPartitionDetail(id) {
    const data = await api(`/api/partitions/${id}`);
    const d = data.assessment.destroy;
    const a = data.assessment.archive;
    main.innerHTML = `
      <div class="toolbar">
        <button type="button" class="secondary" id="btn-back">← 返回分区列表</button>
      </div>
      <section class="panel">
        <h2>分区详情 · ${esc(d.domain_code)} / ${esc(d.biz_date)}</h2>
        <p class="sub">处置策略：<strong>${esc(meta.grace_disposition_label)}</strong>（两种处置互斥，只生效一种）</p>
        <table>
          <tbody>
            <tr><th>存储态</th><td>${tag(d.storage_state)}</td></tr>
            <tr><th>行数</th><td>${esc(d.row_count)}</td></tr>
            <tr><th>生效宽限</th><td>${graceTag(d)}</td></tr>
            <tr><th>人工登记</th><td class="mono">${esc(d.manual_grace_until || "—")}</td></tr>
            <tr><th>只读冻结</th><td>${d.frozen ? `${tag("已冻结")} <span class="mono">${esc(d.freeze_until || "人工解除")}</span>` : "否"}</td></tr>
            <tr><th>销毁可执行</th><td>${reasonList(d.destroy_reasons)}</td></tr>
            <tr><th>归档可执行</th><td>${reasonList(a.archive_reasons)}</td></tr>
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>宽限登记</h2>
        <div class="toolbar">
          <label class="field">宽限截止日<input id="grace-until" value="${esc(d.manual_grace_until || d.grace_until || "")}" /></label>
          <label class="field">原因<input id="grace-reason" value="人工核准" /></label>
          <button type="button" id="btn-grace">登记 / 延长宽限</button>
          <button type="button" class="secondary" id="btn-grace-release">解除人工宽限</button>
        </div>
        <p class="sub">排他写：并发抢推只增不减，短日期不会覆盖他人的长假；覆盖式登记需携带最新 rev=${esc(d.grace_rev)}。</p>
        <table>
          <thead><tr><th>时间</th><th>动作</th><th>申请截止</th><th>生效截止</th><th>操作人</th><th>原因</th></tr></thead>
          <tbody>
            ${
              data.grace_registrations.length
                ? data.grace_registrations
                    .map(
                      (g) => `<tr>
              <td class="mono">${esc(g.created_at)}</td>
              <td>${esc(g.action)}</td>
              <td class="mono">${esc(g.requested_until || "—")}</td>
              <td class="mono">${esc(g.effective_until || "—")}</td>
              <td>${esc(g.operator || "—")}</td>
              <td>${esc(g.reason || "—")}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="6" class="empty">尚无登记</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>依赖列表</h2>
        <div class="toolbar">
          <label class="field">类型
            <select id="dep-type">
              <option>下游消费</option>
              <option>跨域引用</option>
            </select>
          </label>
          <label class="field">引用<input id="dep-ref" placeholder="ads_xxx_daily" /></label>
          <button type="button" id="btn-dep-new">新增阻断依赖</button>
        </div>
        <table>
          <thead><tr><th>类型</th><th>引用</th><th>阻断</th><th>状态</th><th>说明</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.dependencies.length
                ? data.dependencies
                    .map(
                      (dep) => `<tr>
              <td>${esc(dep.dep_type)}</td>
              <td class="mono">${esc(dep.dep_ref)}</td>
              <td>${dep.blocking ? "是" : "否"}</td>
              <td>${tag(dep.status)}</td>
              <td>${esc(dep.note || "—")}</td>
              <td>${dep.status === "生效" ? `<button type="button" class="secondary" data-rel="${dep.id}">解除</button>` : "—"}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="6" class="empty">无依赖</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <section class="panel">
        <h2>只读冻结</h2>
        <div class="toolbar">
          <label class="field">冻结至（留空=人工解除）<input id="ro-until" placeholder="YYYY-MM-DD" /></label>
          <button type="button" id="btn-ro-on">设只读冻结</button>
          <button type="button" class="secondary" id="btn-ro-off">解除冻结</button>
        </div>
        <p class="sub">只读冻结只拦销毁，不拦归档；有限期冻结到期自动失效。</p>
      </section>
      <section class="panel">
        <h2>补采编排</h2>
        <div class="toolbar">
          <label class="field">计划行数<input id="bf-planned" value="${esc(d.row_count)}" /></label>
          <label class="field">宽限天数<input id="bf-days" value="3" /></label>
          <button type="button" id="btn-bf-new">发起补采</button>
        </div>
        <p class="sub">补采入库未完成期间自动挂 blocking 依赖并登记宽限，销毁一律拦截。</p>
        <table>
          <thead><tr><th>编号</th><th>状态</th><th>进度</th><th>宽限至</th><th>操作人</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.backfills.length
                ? data.backfills
                    .map(
                      (b) => `<tr>
              <td>#${esc(b.id)}</td>
              <td>${tag(b.status)}</td>
              <td>
                <div class="progress"><span style="width:${esc(b.progress_pct)}%"></span></div>
                <span class="mono">${esc(b.ingested_rows)} / ${esc(b.planned_rows)}（${esc(b.progress_pct)}%）</span>
              </td>
              <td class="mono">${esc(b.grace_until || "—")}</td>
              <td>${esc(b.operator || "—")}</td>
              <td>${
                b.status === "进行中" || b.status === "未开始"
                  ? `<input class="mini" id="bf-rows-${b.id}" value="${esc(b.planned_rows)}" />
                     <button type="button" class="secondary" data-ingest="${b.id}">上报入库</button>
                     <button type="button" class="secondary" data-complete="${b.id}">完成</button>
                     <button type="button" class="secondary" data-abort="${b.id}">中止</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="6" class="empty">尚无补采任务</td></tr>`
            }
          </tbody>
        </table>
        <h3 class="subhead">补采编排流水（可核）</h3>
        <table>
          <thead><tr><th>时间</th><th>任务</th><th>动作</th><th>入库/计划</th><th>宽限至</th><th>操作人</th><th>备注</th></tr></thead>
          <tbody>
            ${
              (data.backfill_events || []).length
                ? data.backfill_events
                    .map(
                      (e) => `<tr>
              <td class="mono">${esc(e.created_at)}</td>
              <td>#${esc(e.backfill_id)}</td>
              <td>${tag(e.action)}</td>
              <td class="mono">${esc(e.ingested_rows)} / ${esc(e.planned_rows)}</td>
              <td class="mono">${esc(e.grace_until || "—")}</td>
              <td>${esc(e.operator || "—")}</td>
              <td>${esc(e.note || "—")}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="7" class="empty">尚无补采流水</td></tr>`
            }
          </tbody>
        </table>
      </section>
      <div id="detail-feedback"></div>`;

    document.getElementById("btn-back").onclick = () => show("partitions");
    const feedback = (html) => {
      const box = document.getElementById("detail-feedback");
      box.innerHTML = html;
      if (html) box.scrollIntoView({ block: "nearest" });
    };
    const refresh = () => showPartitionDetail(id);
    const guard = async (fn) => {
      try {
        await fn();
        await refresh();
      } catch (err) {
        feedback(errBox(err));
      }
    };

    document.getElementById("btn-grace").onclick = () =>
      guard(async () => {
        const until = document.getElementById("grace-until").value.trim();
        const reason = document.getElementById("grace-reason").value.trim();
        await api(`/api/partitions/${id}/grace`, {
          method: "POST",
          body: JSON.stringify({ until, reason, mode: "extend" }),
        });
      });
    document.getElementById("btn-grace-release").onclick = () =>
      guard(() => api(`/api/partitions/${id}/grace`, { method: "DELETE" }));
    main.querySelectorAll("[data-rel]").forEach((btn) => {
      btn.onclick = () => guard(() => api(`/api/dependencies/${btn.dataset.rel}/release`, {
        method: "POST", body: JSON.stringify({}),
      }));
    });
    document.getElementById("btn-dep-new").onclick = () =>
      guard(() => api(`/api/partitions/${id}/dependencies`, {
        method: "POST",
        body: JSON.stringify({
          dep_type: document.getElementById("dep-type").value,
          dep_ref: document.getElementById("dep-ref").value.trim(),
          blocking: true,
          note: "人工登记",
        }),
      }));
    document.getElementById("btn-ro-on").onclick = () =>
      guard(() => api(`/api/partitions/${id}/read-only`, {
        method: "POST",
        body: JSON.stringify({
          read_only: true,
          until: document.getElementById("ro-until").value.trim() || null,
          reason: "人工只读冻结",
        }),
      }));
    document.getElementById("btn-ro-off").onclick = () =>
      guard(() => api(`/api/partitions/${id}/read-only`, {
        method: "POST",
        body: JSON.stringify({ read_only: false, reason: "人工解除冻结" }),
      }));
    document.getElementById("btn-bf-new").onclick = () =>
      guard(() => api(`/api/partitions/${id}/backfills`, {
        method: "POST",
        body: JSON.stringify({
          planned_rows: Number(document.getElementById("bf-planned").value),
          grace_days: Number(document.getElementById("bf-days").value),
        }),
      }));
    main.querySelectorAll("[data-ingest]").forEach((btn) => {
      btn.onclick = () => guard(() => api(`/api/backfills/${btn.dataset.ingest}/ingest`, {
        method: "POST",
        body: JSON.stringify({
          ingested_rows: Number(document.getElementById(`bf-rows-${btn.dataset.ingest}`).value),
        }),
      }));
    });
    main.querySelectorAll("[data-complete]").forEach((btn) => {
      btn.onclick = () => guard(() => api(`/api/backfills/${btn.dataset.complete}/complete`, {
        method: "POST", body: JSON.stringify({}),
      }));
    });
    main.querySelectorAll("[data-abort]").forEach((btn) => {
      btn.onclick = () => guard(() => api(`/api/backfills/${btn.dataset.abort}/abort`, {
        method: "POST", body: JSON.stringify({}),
      }));
    });
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-17" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <p class="sub">扫描卡片与裁决列表同源：宽限未到期仍开单，但标注拦截，不计为可执行。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>宽限拦截</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${esc(j.id)}</td>
              <td class="mono">${esc(j.as_of_date)}</td>
              <td>${tag(j.status)}</td>
              <td>${esc(j.expired_count)}</td>
              <td>${j.grace_blocked_count ? `<span class="tag bad">${esc(j.grace_blocked_count)}</span>` : "0"}</td>
              <td>${esc(j.message)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="6" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const data = await api("/api/judgments");
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">宽限未到期 / 存在生效依赖 / 补采未完成的分区：执行按钮禁用，接口同样整单拒绝销毁。</p>
        <div id="judgment-feedback"></div>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>宽限</th><th>可执行</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map(
                      (j) => `<tr>
              <td>${esc(j.id)}</td>
              <td class="mono">${esc(j.domain_code)}</td>
              <td class="mono">${esc(j.biz_date)}</td>
              <td>v${esc(j.policy_version)} / ${esc(j.keep_days)}天</td>
              <td>${esc(j.decided_action)}</td>
              <td>${j.decided_action === "销毁" ? graceTag(j) : `<span class="tag">不适用</span>`}</td>
              <td>${reasonList(j.block_reasons)}</td>
              <td>${tag(j.status)}${j.fail_reason ? `<div class="muted">${esc(j.fail_reason)}</div>` : ""}</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? j.can_execute
                    ? `<button type="button" data-exec="${j.id}">执行</button>`
                    : `<button type="button" disabled title="${esc(
                        (j.block_reasons || []).map((r) => r.text).join("；")
                      )}">已拦截</button>`
                  : "—"
              }</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="9" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    const feedback = (html) => {
      document.getElementById("judgment-feedback").innerHTML = html;
    };
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        feedback("");
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
          await renderJudgments();
        } catch (err) {
          feedback(errBox(err));
          await renderJudgments();
          document.getElementById("judgment-feedback").innerHTML = errBox(err);
        }
      };
    });
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-17" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <p class="sub">错位含「已登记宽限却已销毁」的越权情形，两者都计入错位。</p>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${esc(r.id)}</td>
              <td class="mono">${esc(r.as_of_date)}</td>
              <td>${esc(r.online_count)}</td>
              <td>${esc(r.archived_count)}</td>
              <td>${esc(r.destroyed_count)}</td>
              <td>${esc(r.pending_judgment_count)}</td>
              <td>${esc(r.mismatch_count)}</td>
              <td>${esc(r.summary)}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    meta = m;
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
    const banner = document.getElementById("disposition-banner");
    if (banner) {
      banner.textContent = `宽限处置策略：${m.grace_disposition_label}（两种处置互斥）`;
    }
  });

  show(view);
})();
