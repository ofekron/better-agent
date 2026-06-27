🔗 https://github.com/ofekron/better-agent

Let me explain something I built — no jargon, I promise.

**1. The simple idea**

Most AI tools work like this: every time you ask something, a brand-new agent shows up, knows nothing, and you teach it everything from scratch. One question, one agent, start over. Every. Single. Time.

That's wasteful. So I did the obvious thing: **train one agent once, and reuse it.**

In Better Agent this is a *provisioned session*. You teach one base agent a mission — "here's how to search, here's the format, here's the rules." It remembers. Then every new task gets handed to a **fork**: a fresh, cheap copy of that trained agent. The fork does the work, returns the answer, and is thrown away. The base stays trained and clean.

Train once. Fork forever.

**2. Where I use it: the "Ask" feature**

Ask is simple on the surface. You type a task — *"fix the login bug."* Ask searches every session you've ever had, finds the one that's already working on exactly that, and hands your task to *it* — the session that already has the context.

No re-explaining. No starting from zero. Your work lands where it belongs.

**3. Why I built Ask**

After a few months of coding with agents, I had hundreds of sessions. Each one held real context — a repo, a plan, a mission. But every time I started something new, I'd hit the same wall:

*Which of my existing sessions should handle this?*

Half the time, the perfect session already existed. It already knew the code. But I couldn't find it. So I'd start fresh and lose everything I'd already built. Ask exists so the right session finds *you*.

**4. How Ask uses provisioning**

To search hundreds of sessions fast, Ask needs a specialist — one agent whose only job is "scan the transcripts, rank the best match, answer in this exact format."

I provisioned that specialist **once**. Its methodology is baked in. Every time you type into Ask, it **forks** the specialist, the fork runs your search, returns the result, and the fork is destroyed. The base never forgets the format, never gets polluted.

And this week I added the last piece: **the specialist recycles itself.** Every provisioned base now has a `lifetime`. After a set time, it's automatically retired and a fresh one is re-trained from scratch — so the methodology never drifts, never goes stale. It self-heals on a schedule.

One trained base. Infinite disposable forks. Self-recycling.

That's how "start from zero every time" becomes "stand on everything you've already done."

⭐ it, fork it: https://github.com/ofekron/better-agent

#AI #CodingAgents #BuildInPublic #BetterAgent #DeveloperTools
