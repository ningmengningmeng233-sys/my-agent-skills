# UMH 预编译运行包

这是技能内的运行资源，不是源码仓库。请从上一级 README.md / SKILL.md 的 scripts/setup.mjs 和 scripts/umh.mjs 开始，不执行本目录遗留的源码 build/test/package npm scripts。本目录为只读分发包：不提供源码构建/测试/打包入口（package.json 仅声明运行时依赖与引擎约束），不要在此执行任何 npm scripts。

运行需要接收电脑已安装 Node >=24.2、Python >=3.10。node_modules 已附固定依赖。程序不含用户数据与客户端绑定。来源与修改项见上一级 DISTRIBUTION.json；第三方声明见本目录 THIRD_PARTY_NOTICES.md。
