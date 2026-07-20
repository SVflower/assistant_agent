# M23-R2 Agent -> API 交接

> API 必须固定本里程碑最终 Agent main commit（见收尾报告），不得复制 Session/fork 状态机或读取
> Agent 持久文件。

## 版本与入口

- `SESSION_CONTRACT_VERSION == 2`（从 1 提升）。
- Session document schema v2；`EVENT_CONTRACT_VERSION == 1`、RunState/checkpoint v6 不变。
- 公共入口：

```python
from assistant_agent.service import SessionRuntime, SessionSnapshot

snapshot: SessionSnapshot = session_runtime.snapshot()
forked: SessionSnapshot = session_runtime.fork_session(
    before_user_message_id,
    idempotency_key,
)
```

`fork_session` 已绑定源 Session，不接受 source ID、历史数组、assistant ID、Run ID 或 offset。

## DTO

```text
PublicMessageSnapshot {
  id: msg_[a-f0-9]{24}
  role: user | assistant
  created_at: UTC string | null
  reply_to_message_id: msg_* | null
  content: string
  artifacts: tuple[PresentationArtifactRef, ...]
}

SessionSnapshot {
  id, schema_version=2, title, title_source, metadata_version,
  created_at, updated_at,
  messages: tuple[PublicMessageSnapshot, ...],
  artifacts: tuple[PresentationArtifactRef, ...],
  assistant_messages: compatibility projection,
  fork_created: true | false | null
}
```

user reply 固定 null；assistant reply 必须指向同 snapshot user。`PresentationArtifactRef.run_id` 现为
nullable additive 字段；fork Artifact 为 null，普通 Run Artifact 仍为字符串。

## API 必改

1. 启动依赖检查从 Session contract 1 改为 2；保留 Event 1、RunState 6。
2. Session GET/list/export 保真映射 Agent `messages` 的 id/time/reply/artifacts，不再兼容生成临时消息 ID。
3. 新增 `POST /api/v1/sessions/{session_id}/forks`：严格 `application/json`、body 仅
   `before_user_message_id`，header `Idempotency-Key` 为 1..200 visible ASCII。
4. 载入源 `SessionRuntime` 后调用 `fork_session`；`fork_created=true` 返回 201，false 返回 200。
5. 不扫描 Session/Artifact 文件，不复制 history/Artifact，不创建 Run。fork 成功后的 edit/regenerate
   由 Web 再调用现有普通 Run API。
6. `run_id=null` 必须原样输出；不得伪造源 Run ID。

## 错误映射

| Agent code | HTTP |
|---|---:|
| `invalid_idempotency_key` | 400 |
| `user_message_not_found` | 404 |
| `idempotency_conflict` | 409 |
| `session_migration_required` | 409 |
| `invalid_fork_request` | 422 |
| `session_unavailable` | 503 |

源 Session 不存在继续映射 `session_not_found` 404。跨 Session message ID 必须统一
`user_message_not_found`，不得泄漏目标 Session 是否存在。

## 联调测试

- v1 Session 首次 GET 后 schema v2、ID/reply/time 稳定，API 重启不变化。
- 第一/中间/末尾 user fork；目标严格排除边界，compaction 摘要不泄漏。
- Chart Artifact 新 ID/Session/message、`run_id=null`、源 Artifact 不变且可分别 GET。
- 同 key 同请求首次 201、重放/Agent 重启后 200 且同目标；同 key 异参 409。
- 并发重复请求只产生一个目标；失败不出现半成品 catalog 项。
- edit/regenerate 的 fork 与后续 Run 是两个步骤；Run 失败保留 fork Session，重试只重试 Run。
