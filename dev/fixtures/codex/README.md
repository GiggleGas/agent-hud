# Codex 结构样本

这三个样本从本机真实 Codex Desktop 记录抽取，只保留解析器需要的字段；它们来自不同回合，不是同一段完整对话。正文、问题、选项、回答、目录、账号、会话和工具调用编号均已替换为固定测试值，时间已平移为固定值。

- `desktop.jsonl`：桌面主任务开始和完成。原记录的 `started_at` 为整数秒数。
- `child.jsonl`：桌面子任务的父任务关联和完成事件。这个样本没有开始事件，完成事件里的 `started_at` 提供本轮开始时间。
- `question.jsonl`：真实的 `request_user_input` 调用、回答和任务完成顺序。

当前电脑未找到独立 Codex CLI 的真实记录，不能把 Desktop 的 `source=exec` 记录当成独立 CLI 样本。CLI 仍由 `test_cli_events` 的构造数据验证；补充真实 CLI 样本仍待后续验证。毫秒、时间字符串及无法识别的 `started_at` 也由独立构造用例验证。
