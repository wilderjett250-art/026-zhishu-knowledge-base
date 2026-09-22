# PKAS 检索评测协议

PKAS 的评测分成三层，避免把“搜到了来源”和“答案写得好”混成一个数字。

## 1. 检索层：固定问题集对照

同一批带来源标注的问题，分别运行：

- A：SQLite FTS 全文检索；
- B：Qdrant 向量检索；
- AB：全文与向量混合检索；
- AB+Rerank：只作为额外实验，不作为默认基线。

默认脚本是：

```powershell
uv run python scripts/run_rag_benchmark.py
```

输出文件：`reports/rag-benchmark-latest.json`（仅本机生成，不进入 Git）。

主要指标：

- `Hit@5`：前 5 条是否至少有一个正确来源；
- `Source-located Recall@5`：已知正确来源有多少被找回；
- `MRR`：第一个正确来源排得有多靠前；
- `nDCG@5`：同时考虑相关性和排名。

银标题的来源列表不保证穷尽所有正确来源，所以银标不宣称完整 Precision；Precision 必须来自人工逐结果标注。

## 2. 人工金标层：校准 Precision

人工逐题检查候选来源，至少标记：相关、部分相关、不相关。然后使用 PKAS 已有的 `run_eval` 计算 Precision、Recall、MRR、nDCG，并检查 `judgment_coverage=100%`。当前只有完成人工复核的题才进入正式 Precision 指标。

## 3. 回答层：RAGAS 风格

当 PKAS 负责生成最终回答时，再增加：

- Context Precision：送入回答模型的上下文有多少真正有用；
- Context Recall：回答所需事实有多少被上下文覆盖；
- Faithfulness：回答中的事实是否能被来源支持；
- Answer Relevancy：回答是否真正回应问题。

当前 PKAS 的主职责是为 Codex/MCP 提供来源检索，因此先把检索层和人工金标层跑稳，回答层不使用自动模型评分冒充人工事实。

参考方法：[BEIR](https://arxiv.org/abs/2104.08663)、[Ragas 指标说明](https://docs.ragas.io/en/latest/concepts/metrics/available_metrics/)。
