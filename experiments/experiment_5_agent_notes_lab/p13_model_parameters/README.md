# p13 大模型参数设置

对应顶层笔记第 7 章。原型用固定 logits 和固定随机种子模拟采样，专门展示参数改变的是“分布”，不是给模型增加新的知识。

## 参数关系

- `temperature`：调节分布尖锐程度；低温更稳定，高温更多样。
- `top_p`：保留累计概率达到 `p` 的最小候选集合。
- `max_tokens`：输出长度上限，结构化输出要预留足够空间。
- `presence_penalty`：出现过就轻微惩罚，鼓励引入新内容。
- `frequency_penalty`：按出现次数累积惩罚，抑制重复。

一般只需要先根据任务设置 temperature，再小幅调整 top-p 或 penalty；不要同时把所有旋钮拧到最大。分析型 Agent 通常优先稳定、准确、结构完整，而不是追求随机性。

## 建议断点

- `parameter_lab.py:distribution`：看 penalty 如何改 logits，top-p 如何截断候选。
- `parameter_lab.py:_softmax`：看 temperature 如何改变概率分布。
- `parameter_lab.py:generate`：看固定 seed 下参数如何导致输出差异。

运行：

```text
python experiments/experiment_5_agent_notes_lab/p13_model_parameters/main.py
```

`production.py` 的 `SamplingConfig` 对 temperature、top-p、max tokens 和 seed 做启动时校验，`ProductionSampler` 通过显式随机源保证实验可复现。
