"use client";

import { useEffect, useMemo, useState } from "react";
import workflowDefinition from "../docs/workflow.definition.json";

const STORAGE_KEY = "dub-workbench-manual-gates-v1";

type ManualGate = (typeof workflowDefinition.manual_gates)[number];

type WorkflowPanelProps = {
  apiBase: string;
};

function agentName(agentId: string): string {
  return workflowDefinition.agents.find((agent) => agent.id === agentId)?.short_name || agentId;
}

function loadSavedGates(): Set<string> {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY);
    const parsed = value ? JSON.parse(value) : [];
    return new Set(Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === "string") : []);
  } catch {
    return new Set();
  }
}

export default function WorkflowPanel({ apiBase }: WorkflowPanelProps) {
  const [completedGates, setCompletedGates] = useState<Set<string>>(new Set());

  useEffect(() => {
    // This non-sensitive local checklist is restored only in the current browser profile.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCompletedGates(loadSavedGates());
  }, []);

  const requiredGates = useMemo(
    () => workflowDefinition.manual_gates.filter((gate) => !("optional" in gate && gate.optional)),
    [],
  );
  const requiredDone = requiredGates.filter((gate) => completedGates.has(gate.id)).length;
  const requiredProgress = Math.round(requiredDone / requiredGates.length * 100);
  function setGate(gate: ManualGate, checked: boolean) {
    const next = new Set(completedGates);
    if (checked) next.add(gate.id);
    else next.delete(gate.id);
    setCompletedGates(next);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([...next]));
  }

  function resetGates() {
    setCompletedGates(new Set());
    window.localStorage.removeItem(STORAGE_KEY);
  }

  return (
    <section className="workflowSurface" aria-labelledby="workflow-title">
      <div className="workflowHero">
        <div className="workflowHeroCopy">
          <p className="eyebrow">生产流程 / 本地可恢复</p>
          <h1 id="workflow-title">一条流程，<em>八个交接点。</em></h1>
          <p>
            下载、翻译、双 Agent 审核、选音、付费配音、画面重定时与验收共用同一套状态模型。
            自动任务可以续跑；Cookie、音色、费用和最终交付始终由你确认。
          </p>
          <div className="workflowHeroActions">
            <a href={`${apiBase}/docs/LOCAL_DUBBING_WORKFLOW.md`} target="_blank" rel="noreferrer">打开完整运行手册 <span>↗</span></a>
            <span>定义文件：workflow.definition.json</span>
          </div>
        </div>
        <aside className="testedCase" aria-label="已验证案例">
          <div className="caseStamp"><span>已验证案例</span><strong>{workflowDefinition.tested_case.video_id}</strong></div>
          <dl>
            <div><dt>成片</dt><dd>{workflowDefinition.tested_case.output_duration}</dd></div>
            <div><dt>画质</dt><dd>{workflowDefinition.tested_case.resolution}</dd></div>
            <div><dt>字幕</dt><dd>{workflowDefinition.tested_case.subtitle_count.toLocaleString("zh-CN")} 条</dd></div>
            <div><dt>TTS</dt><dd>{workflowDefinition.tested_case.tts_batches} 批</dd></div>
          </dl>
          <p>完整画面 · 中文原速 · 8% 原声 · 烧录与软字幕</p>
        </aside>
      </div>

      <div className="workflowSummaryBar">
        <div><strong>{workflowDefinition.team.recommended_agents}</strong><span>个 Agent 角色</span></div>
        <div><strong>{workflowDefinition.team.peak_concurrency}</strong><span>个峰值并发</span></div>
        <div><strong>{workflowDefinition.stages.length}</strong><span>个生产阶段</span></div>
        <div><strong>{requiredGates.length}</strong><span>个必需人工关卡</span></div>
        <p>{workflowDefinition.team.note}</p>
      </div>

      <div className="workflowGrid">
        <div className="workflowMainColumn">
          <div className="workflowSectionHead">
            <div><p className="eyebrow">阶段地图</p><h2>从链接到成片的唯一主路径</h2></div>
            <span className="workflowLegend"><i className="autoDot" /> 自动 <i className="manualDot" /> 人工关卡</span>
          </div>

          <div className="workflowRail">
            {workflowDefinition.stages.map((stage) => {
              const stageGates = workflowDefinition.manual_gates.filter((gate) => gate.stage === stage.number);
              const owners = [stage.owner, ...("co_owners" in stage ? stage.co_owners : [])];
              return (
                <details className="workflowStage" key={stage.id}>
                  <summary>
                    <span className="stageNumber">{stage.number}</span>
                    <span className="stageTitle"><small>{stage.mode}</small><strong>{stage.title}</strong><em>{stage.summary}</em></span>
                    <span className="stageOwners">{owners.map((owner) => <b key={owner}>{agentName(owner)}</b>)}</span>
                    <span className="stageChevron" aria-hidden="true">＋</span>
                  </summary>
                  <div className="stageBody">
                    <div>
                      <h3>执行内容</h3>
                      <ol>{stage.tasks.map((task) => <li key={task}>{task}</li>)}</ol>
                    </div>
                    <div className="stageIO">
                      <section><h3>输入</h3>{stage.inputs.map((item) => <span key={item}>{item}</span>)}</section>
                      <section><h3>产物</h3>{stage.outputs.map((item) => <span key={item}>{item}</span>)}</section>
                    </div>
                    <div className="resumeNote"><strong>断点</strong><span>{stage.resume_from}</span></div>
                    {stageGates.length > 0 && (
                      <div className="stageGateList">
                        <strong>本阶段人工确认</strong>
                        {stageGates.map((gate) => (
                          <span key={gate.id} className={completedGates.has(gate.id) ? "done" : ""}>
                            {completedGates.has(gate.id) ? "✓" : "○"} {gate.label}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </details>
              );
            })}
          </div>
        </div>

        <aside className="workflowSideColumn">
          <section className="manualGatePanel" aria-labelledby="manual-gates-title">
            <div className="gatePanelHead">
              <div><p className="eyebrow">人工关卡</p><h2 id="manual-gates-title">你来做决定</h2></div>
              <strong>{requiredDone}/{requiredGates.length}</strong>
            </div>
            <div className="gateProgress" aria-label={`必需人工关卡已完成 ${requiredProgress}%`}><i style={{ width: `${requiredProgress}%` }} /></div>
            <p className="gatePrivacy">勾选记录只保存在当前浏览器，不包含 Cookie 或 API Key。</p>
            <div className="gateChecklist">
              {workflowDefinition.manual_gates.map((gate) => {
                const optional = "optional" in gate && gate.optional;
                return (
                  <label key={gate.id} className={completedGates.has(gate.id) ? "checked" : ""}>
                    <input type="checkbox" checked={completedGates.has(gate.id)} onChange={(event) => setGate(gate, event.target.checked)} />
                    <span><strong>{gate.label}{optional ? "（可选）" : ""}</strong><small>{gate.why}</small></span>
                    {gate.sensitive && <em>敏感</em>}
                  </label>
                );
              })}
            </div>
            <button type="button" className="resetGates" onClick={resetGates} disabled={completedGates.size === 0}>重置本地勾选</button>
          </section>

          <section className="handoffRule">
            <span>交接规则</span>
            <p><strong>先验收产物，再启动下一阶段。</strong>失败时从最近的 JSON、manifest 或媒体区块继续，不回到视频开头重做。</p>
          </section>
        </aside>
      </div>

      <section className="agentBoard" aria-labelledby="agent-board-title">
        <div className="workflowSectionHead">
          <div><p className="eyebrow">Agent 编制</p><h2 id="agent-board-title">谁在什么时候接手</h2></div>
          <p>审核 A、B 必须独立覆盖全文；质检 Agent 不参与生成。</p>
        </div>
        <div className="agentCards">
          {workflowDefinition.agents.map((agent, index) => (
            <article key={agent.id} className={`agentCard agentColor${index}`}>
              <div className="agentCardTop"><span>{String(index + 1).padStart(2, "0")}</span><small>阶段 {agent.active_in.join(" · ")}</small></div>
              <h3>{agent.name}</h3>
              <p>{agent.purpose}</p>
              <div>{agent.deliverables.map((item) => <span key={item}>{item}</span>)}</div>
            </article>
          ))}
        </div>
        <div className="agentPeak">
          <span>A</span><span>B</span><span>主</span>
          <p><strong>峰值并发只出现在翻译审核。</strong>其他媒体任务顺序接力，避免 GPU、磁盘和付费 API 互相争用。</p>
        </div>
      </section>
    </section>
  );
}
