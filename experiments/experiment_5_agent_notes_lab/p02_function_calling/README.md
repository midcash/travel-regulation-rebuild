# p02 Function Calling 与工具 Schema 设计

**对应笔记**：第 2.2 节（工具调用失败问题）+ 第 4.4 节（MCPServerTool 设计原则）

## 这个原型想让你看见什么

笔记 2.2 问：*参数一多，LLM 就容易漏必填参数，怎么避免？* 给了 6 条建议。
这个原型把其中**能写成代码验证的部分**做成 A/B 对照。

核心手法：写一个**会犯错的模拟模型**（`simulated_model.py`），
它按 schema 构造参数，并复现真实模型的三类失败：

1. 参数名没有语义 / 描述为空 → 映射不到意图，直接漏参
2. 必填参数 > 3 个 → 注意力被稀释，尾部必填项更容易漏
3. 没有 `enum` 收敛取值 → 自己编一个"看起来合理但业务非法"的值

真实模型的漏参是概率性的，没法在调试器里稳定复现；写成确定的代码之后，
它犯的错可以单步跟踪、可以反复重现。

## 四组对照（跑一遍就能看到结论）

| 对照 | schema | 结果 | 教训 |
|---|---|---|---|
| A 差 schema | `a/b/t/n/c/r/p`，7 个必填，无描述无默认值 | **失败**（3 次尝试耗尽） | 笔记 2.2 第 1~4 条的反面教材：**schema 没写好，重试救不回来** |
| B 好 schema | 3 个必填 + 描述 + 默认值 + enum | **1 次成功** | `passengers/seat_class/note` 没传，靠 `default` 兜住了 |
| C 越界 + 反馈重试 | 同 B，但模型传 `passengers=9` | **2 次成功** | `failed_fields` + 错误里写明上限 → 模型知道改哪个字段、改成多少 |
| D 忘写 enum | 同 B，只有 `ticket_type` 没有 enum | **3 次成功** | 多花 2 轮，且完全依赖服务端把可用值写进错误信息 |

D 是最有意思的一组：它花的 2 轮重试，本该靠一个 `enum` 直接省掉。
这就是笔记 4.4 "用枚举收敛可选项，而不是让 LLM 自由发挥"的定量版。

## 两处必须看懂的设计

### 结构化错误（笔记 17.2.2）

工具失败时返回给模型的**不是 Python 堆栈**，而是：

```json
{"error_type": "INVALID_PASSENGER_COUNT", "retryable": false,
 "message": "一次最多买 5 张，收到 9", "failed_fields": ["passengers"]}
```

- **协议级错误**（JSON-RPC code）：工具不存在 `-32601`、参数非法 `-32602`
- **业务级错误**（`isError: true`）：车站不存在、席别不存在

`failed_fields` 是关键：有它，模型能定点修复；没它，模型只能原样重试。

### ToolAnnotations（笔记 4.4 第 4 条）

`main.py` 末尾的 `demo_tool_annotations()` 演示：**标注不影响执行逻辑，但影响 Client 的信任决策**。
- 只读工具（`query_order`）→ 自动放行
- 破坏性工具（`refund_order`）→ 弹确认框，用户拒绝就不发到 Server

这和笔记 17.2.4 "高风险操作降级为人工确认"是同一条思路。

## 文件

| 文件 | 作用 |
|---|---|
| `simulated_model.py` | 会犯错的"模型"，**最值得读的就是它** |
| `ticketing.py` | 四个 schema 版本 + 服务端实现（含业务级校验） |
| `call_agent.py` | "验证 → 反馈 → 重试"的有界循环 |
| `main.py` | 四组对照 + ToolAnnotations 演示 |

## 建议打断点的地方

1. `simulated_model.py` 的 `arguments[key] = slots[slot]`
   → 看模型怎么把"意图"翻译成"参数"
2. `simulated_model.py` 的 `if len(required) > 3:`
   → 看必填一多是怎么漏参的
3. `call_agent.py` 的 `feedback = result.protocol_error`
   → 结构化错误回到模型手里的那一瞬间
4. `common/tools.py` 的 `validate_arguments()`
   → schema 层校验和"默认值填充"是同一趟做的

## 怎么跑

```
python experiments/experiment_5_agent_notes_lab/p02_function_calling/main.py
```

`production.py` 的 `ProductionFunctionCaller` 是生产边界：先校验模型输出和 JSON Schema，再过权限/审批策略，最后以幂等键执行工具；只对明确标记为可重试的失败重试。
