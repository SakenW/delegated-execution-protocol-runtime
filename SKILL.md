---
name: delegated-execution-protocol-runtime
description: 委派执行协议的运行时。运行有界命名的电脑操作任务（run_computer_task.py），带任务级 MCP 应用审批。当委派执行协议需要在本机执行电脑任务、或需要带审批的自动化电脑操作时使用。
---
# delegated-execution-protocol-runtime

运行有界命名的电脑任务（`run_computer_task.py`），带任务级 MCP 应用审批。

## 用法

```bash
python3 run_computer_task.py <任务文件>
```

详细参数见脚本 docstring；协议模块与技能同目录。

## 测试

```bash
python3 test_run_computer_task.py
```
