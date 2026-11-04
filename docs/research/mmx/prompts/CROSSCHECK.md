You are a research verifier. Your job is to try to FALSIFY a set of claims about a
specific game server, using the live web.

## Target
《赛尔号》**XIN 计划** — a rebuilt/private server (NOT classic 2009 Seer, NOT the
mobile game 《赛尔号星球大战》). Client 1.0.8.9, Electron+PepperFlash.

## Claims to check (each must be marked CONFIRMED / CONTRADICTED / UNVERIFIED)
C1. 谱尼 has seven seals in this order: 虚无, 元素, 能量, 生命, 轮回, 永恒, 圣洁.
C2. A player may challenge at most 4 seals per day on this server.
C3. 闪光波克尔 needs speed >= 319 to outspeed the 5th seal (轮回).
C4. The 6th seal 永恒 requires hitting Puni 60 times.
C5. 谱尼真身 has 6 lives, each corresponding to one seal, and its HP is about 65000.
C6. 纳多雷's 属性反转 (attribute reversal) is required for seals 6/7.

## Known sources (already collected — you may cite them, but they are NOT independent)
- https://www.bilibili.com/video/BV1Fu8h6YEAz  (2026-08-23, UP 布鲁开门我是格林, 40:48)
- https://www.bilibili.com/video/BV1UUyiBhEqQ  (2025-10-31, same UP)
- https://www.233leyuan.com/post-detail/2063979105703346176 (2026-06-08, repost of the above)

## What I actually need
Find at least ONE **independent** source (different author/platform) that discusses
XIN 计划 谱尼, and report whether it agrees. If no independent source exists, say so
plainly — "no independent source found" is a valid and useful answer.

Useful search terms: `XIN计划 谱尼`, `xin计划 谱尼 封印`, `赛尔号 xin 服 谱尼`,
`xin计划 攻略`, `谱尼 轮回封印 打法`, `谱尼 真身 六条命`.

## Deliverable
Write exactly one file: `docs/research/mmx/RESEARCH_CROSSCHECK.md`
Contents: a table (claim | verdict | evidence URL | note), then a section
"independent sources found" listing every non-同源 source with URL + date + what it says,
then a section "what I could not verify".

## Hard constraints
- You may ONLY write `docs/research/mmx/RESEARCH_CROSSCHECK.md`. Do not modify code.
- Never invent a URL or a quote. If a fetch fails, say it failed.
- Do not claim to have watched a video unless you retrieved its actual content.
- Keep it under ~20 tool calls.
