# 输出契约三档模板（v7.4.2 从 SKILL.md §5 挪入）

三档选择的短合同仍在 `SKILL.md` §5；本文件只放可直接复制的输出模板。

## A. 直接模式（默认纯净输出）

````markdown
【参考素材顺序】（有参考图时展示）
1. 图1：沈昭角色图（锁定黑袍、束发与青铜剑）
2. 图2：本组分镜宫格

【最终提示词】
```text
可直接复制的纯提示词正文
```

【校验凭证】
- 模式：native-drama-series | 规则集版本：与 config/version.json 一致（当前 7.4.4）
- 审计凭据：
  未执行审计脚本时必须真实声明未运行，严禁虚假 PASS：
  ```text
  AUDIT_STATUS: NOT_RUN
  AUDIT_TOTAL: null
  AUDIT_PASSED: null
  AUDIT_FAILED: null
  ```
  真正调用 audit_cli 静态门禁通过后，输出实际数值与哈希：
  ```text
  AUDIT_STATUS: PASS
  AUDIT_TOTAL: <实际值>
  AUDIT_PASSED: <实际值>
  AUDIT_FAILED: 0
  PROMPT_SHA256: <实际哈希>
  ```
````

## B. 标准模式（设计思路解析）

在直接模式前增加：【创作理解】（一句话镜头动机）、【系统关键假设】（最多3条时空预设）、
【保留的用户硬约束】（绝对保留要素）。

## C. 制作模式（多镜头连戏协作）

输出连续镜头台账：镜头组清单、每组独立提示词、上一镜末态与下一镜起态物理交接。
