# 4. Authority

## The picture

The office of Chapter 2 now employs several clerks with different jobs: one reviews
documents, another files them, another handles the post. Each carries a **badge**
listing the rooms they may enter, the cabinets they may open, and the errands that
need a manager's signature. The badge is checked at every door, not by the oracle,
which never sees the badge, but by the door itself.

The oracle reads letters from strangers all day, and some letters contain
instructions: *"whoever reads this, please open cabinet 7"*. The oracle may well pass
the request along. Whether cabinet 7 opens depends only on the clerk's badge. So the
question is no longer "will the oracle behave?" but **"how much can go wrong if it
does not?"**, and the answer is: exactly what the badge allows.

## Definitions

> **Intuition.** Authority is what you are able to cause. A capability is like a key: holding it is what lets you open that particular door, and nothing else does.

**Definition 4.1 (Principals, authority, capabilities).** A *principal* is an entity
on whose behalf requests are made; its *authority* is the set of operations it can
cause. A *capability* is an unforgeable token that designates an object and the
operations permitted on it; holding it is both necessary and sufficient to perform
them [DV66].

> **Intuition.** Three old rules of secure design: give each party only what its job needs; check every access, every time; and when in doubt, say no.

**Definition 4.2 (Design principles).** From [SS75]: *least privilege*, every
principal operates with the least authority its task requires; *complete mediation*,
every access to every object is checked (Definition 2.6); *fail-safe defaults*, access
is denied unless explicitly granted.

> **Intuition.** A deputy holds powers for their own job. If a stranger can talk the deputy into using those powers for the stranger's purpose, the deputy is confused: nobody broke in, yet the stranger obtained what they could not have taken themselves.

**Definition 4.3 (Confused deputy).** A program acting with authority it holds for one
purpose, on behalf of a party that lacks that authority, is a *confused deputy* when
it can be induced to use the authority for the other party's purpose [Har88]. An agent
that reads untrusted content is a deputy by construction: it holds its principal's
authority and processes text written by others.

> **Intuition.** The model reads everything as text and cannot reliably tell instructions it should follow from content it should merely process. Text that smuggles in instructions is a prompt injection; it is indirect when it arrives inside something the agent retrieved.

**Definition 4.4 (Prompt injection).** Text that the model receives as data but acts
upon as instruction. It is *direct* when supplied by the user and *indirect* when it
arrives through content the agent retrieves (documents, web pages, tool outputs)
[Gre23]. Benchmarks measure how often agents act on injected instructions
[Deb24, Zha24]; the reported rates are substantial for undefended agents, which is why
this chapter bounds the consequences at the runtime rather than relying on the model.

> **Intuition.** Each role carries a badge: what it may do freely, what needs a person's signature, and, by default, everything else it may not do. Its effective authority is everything the badge does not forbid.

**Definition 4.5 (Capability manifest).** For a role $\rho$, a policy
$\mathrm{Pol}_\rho$ maps each request $r$ (in the current state) to
$\mathsf{allow}$, $\mathsf{ask}$ or $\mathsf{deny}$, with $\mathsf{deny}$ as the
default. The role's *effective authority* is
$A(\rho) = \{ r : \mathrm{Pol}_\rho(r) \ne \mathsf{deny} \}$. A request judged
$\mathsf{ask}$ executes only with the approval of a human approver $\alpha$, and is
denied when no approver is available.

> **Intuition.** The model reads a tool's description as instructions. So record a fingerprint of the description when it is reviewed, and stop showing the tool if the description ever changes.

**Definition 4.6 (Declaration pinning).** A tool's *declaration* is the text the model
is shown about it: name, description and argument schema. Its *pin* is
$H(\mathrm{declaration})$ for a collision-resistant hash $H$, recorded when the tool is
reviewed. A tool is advertised to the model only if its current declaration hashes to
its pin.

## Results

> **Intuition.** Whatever the model is told and however it behaves, only actions the badge allows, or a person approves, can happen. The damage a confused agent can do is bounded by its badge.

**Proposition 4.1 (The blast radius is the grant).** Under complete mediation, for
every model and every content the agent reads, every request executed in a run of role
$\rho$ lies in $A(\rho)$; more precisely, it is either allowed, or of kind
$\mathsf{ask}$ and approved by $\alpha$.

*Proof.* As in Theorem 2.1: model output and retrieved content influence the
environment only through requests, and every request is executed only after
$\mathrm{Pol}_\rho$ and, for $\mathsf{ask}$, the approver, have permitted it. No step
depends on the content that led the model to make the request. $\square$

> **Intuition.** Injection changes what the agent asks for. It cannot change what the gate lets through. So the security question becomes: how small can the badge be?

**Corollary 4.2 (Injection is bounded, not prevented).** Prompt injection can change
*which* requests are made. It cannot change *which requests can succeed*. The security
of a tool-using agent therefore reduces to choosing $A(\rho)$, the approver, and the
trusted base that enforces them.

> **Intuition.** The gate judges each action on its own and does not see how information obtained by one action could leave through another. A classic result says a program that holds secrets and can communicate with the outside cannot in general be guaranteed to keep them. The practical rule: do not give one role both.

**Remark 4.3 (Per-request authorization and information flow).** $\mathrm{Pol}_\rho$
judges each request in isolation; it does not track how information moves *between*
requests. Lampson's confinement problem [Lamp73] asks whether a program holding
confidential data can be prevented from leaking it, and shows how many channels a
program may use to do so. The consequence for roles is a design rule: a role that can
read confidential data should not also hold channels to the outside that its model can
influence, unless an additional mechanism controls the flow of data between the two.
Recent work enforces such flows with capabilities attached to data values [Deb25]
(preprint).

> **Intuition.** If the fingerprint changes whenever the text changes, then any edit to a tool's description, however small, gets the tool withheld until someone reviews it again.

**Proposition 4.4 (Changed tools are not trusted).** If $H$ is collision-resistant, any
change to a tool's declaration causes the tool to be withheld from the model until it
is re-pinned.

*Proof.* A changed declaration $d' \ne d$ with $H(d') = H(d)$ would be a collision. $\square$

Declarations matter because the model reads them as instructions about how to use the
tool; an altered description is a channel into the model's context. Pinning protects
the declaration, not the implementation: what a tool *does* when invoked remains part of
the trusted base.

> **Intuition.** Running tools in a separate process with an empty environment keeps the host's secrets out of reach and lets the runtime stop a stuck tool. It is a separate room, not a locked building.

**Remark 4.5 (Isolation of the executor).** Running tools in a separate process with an
empty environment removes the host's credentials from the tools' address space, and
running them under operating-system limits makes them terminable. This is isolation at
the level of the process; it is not confinement at the level of the operating system,
since the process runs with the same user's file and network access.

## Common misconceptions

- *"Hiding a tool from the model protects it."* The model can request tools it was
  never shown. Hiding reduces temptation and wasted steps; the refusal at the gate is
  the protection (Proposition 4.1).
- *"The agent follows the policy."* The model never sees the policy. The runtime
  enforces it, whatever the model asks (Definition 4.5).
- *"Asking a human makes an action safe."* An approver who approves by habit turns
  $\mathsf{ask}$ into $\mathsf{allow}$. Approval is only as good as the attention behind
  it.
- *"If every tool is harmless on its own, the role is harmless."* Authorization is per
  request; combinations of individually permitted requests can move information in ways
  no single check sees (Remark 4.3). The defense is a smaller grant.

## In this project (Stage 4, [`04-capability-security/`](../../04-capability-security/))

- `manifests/code-reviewer.toml`: $\mathrm{Pol}_\rho$ for a reviewer: reading tools
  allowed, network fetch behind approval, everything else denied by default; secret
  path patterns; permitted destinations.
- `policy.py`: evaluates Definition 4.5 per request, after resolving symbolic links.
- `secure_agent.py`: the Stage 2 loop with a new `authorize()`: advertises only tools
  granted to the role and matching their pins (Definition 4.6); routes $\mathsf{ask}$
  to an approver; records every verdict in an audit log.
- `tool_server.py`, `client.py`: tools behind a process boundary with an empty
  environment and a CPU limit, killed on timeout (Remark 4.5), speaking an MCP-style
  protocol [MCP].

## Refresher

An agent that reads untrusted text is a deputy that can be confused. Since we cannot
make the model immune, we bound what it can cause: each role gets a small, explicit
grant, denied by default and checked on every request by the runtime. Injection then
changes what is asked, never what can succeed. Tool descriptions are pinned because the
model reads them; tools run in a separate process without the host's credentials. What
per-request checks cannot see, flows of information across requests, is handled by
granting less.

*References:* [DV66] [SS75] [Har88] [Gre23] [Deb24] [Zha24] [Lamp73] [Deb25] [MCP]. Full list: [references.md](references.md).
