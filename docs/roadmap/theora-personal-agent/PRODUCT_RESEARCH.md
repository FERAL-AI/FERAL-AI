# Theora product landscape and product-market-fit research

Research date: September 30, 2026. Official vendor pages and FDA sources were checked through web search/open. This is planning research, not independent product testing, a market-size estimate, regulatory advice, or proof of product-market fit. All recommended segments, thresholds and experiments below are hypotheses to validate.

## Main conclusion

A general agent, conversational memory and biometric coaching already have substantial competition. Theora's strongest candidate position is the combination of user-owned long-term memory, glasses-based real-world context and health signals, and useful actions across phone and computer. Whether that combination is valuable depends on proving repeated outcomes with current hardware. Being open source is an adoption and trust advantage, not sufficient evidence of demand.

Suggested initial promise: "Remember what matters, help me follow through, and put my day in context." Keep the full personal-agent ambition as the platform vision while testing one repeatable daily loop.

## Verified landscape

| Product | Verified official positioning and current status | Planning implication |
|---|---|---|
| Meta Muse | Official page describes mobile/Mac/WhatsApp conversations, goals and background work, connectors and a persistent isolated Linux VM/browser. Mac download is offered; free usage has limits with paid expansion. | A broad agent with memory, voice and app actions is already a competitive baseline. VM Linux is not evidence of a native Linux client. |
| Muse on Meta glasses | Meta's September 24, 2026 announcement says Muse launched earlier in September and glasses access is coming in subsequent months. Muse Charm is announced with more information later this year. | Separate launched agent from announced glasses integration. Do not present every demonstrated feature as shipping to every account. |
| Meta AI desktop | Official Mac page offers shortcut access, window context and dictation. | "ChatGPT-like native feel" should translate into low-friction daily access, not only a standalone chat window. |
| Dot / New Computer | Official site announces winding down and data export, stating Dot remains operational until October 5. The notice has no explicit year; footer says 2023-2025. | Treat Dot as a historical experience reference, not a confirmed presently available competitor. The shutdown underscores portable memory and user continuity. |
| Omi | Official site markets conversation capture, summaries, tasks and memory across iPhone, Android, Mac and web, offline recording and Omi Glass. Main source repository is public and MIT licensed. | Open-source wearable memory is not unique to Theora. Study capture reliability, onboarding and useful follow-through. Product claims were not independently measured. |
| Oura Advisor | Support page last updated June 11, 2026 lists Gen3+ with active membership on iOS/Android; personalized biometric conversation and stored memories. Users can view/delete memories or reset Advisor; processing is cloud-based. | Biometric chat and persistent health context are already shipping. Memory controls belong in the primary product. |
| WHOOP AI guidance | Official April 30, 2026 page describes coaching from sleep, strain, recovery, goals and routines. | Explanations and recovery coaching alone are a crowded proposition. An actionable cross-device loop may differentiate more. |
| Limitless | Main official site failed multiple fetch attempts; help homepage rendered minimal content. Secondary material consistently describes Meta acquisition and curtailed availability, but current official sales/support terms could not be directly verified in this research. | Treat as a historical reference and unresolved availability item. Do not quote current price or recommend buying. Do not use the now-unaffiliated rewind.ai as an authoritative original-company source. |

Sources, accessed September 30, 2026:

- [Muse product page](https://ai.meta.com/muse/). Search returned substantive page text; direct open rendered empty. No exhaustive region/account eligibility check.
- [Meta Connect announcement, September 24, 2026](https://about.fb.com/news/2026/09/the-biggest-news-from-connect-2026/).
- [Meta AI for Mac](https://ai.meta.com/meta-ai/download/).
- [New Computer winding-down notice](https://new.computer/). Date ambiguity explicitly retained above.
- [Omi product page](https://www.omi.me/), [Omi official overview, January 6, 2026](https://help.omi.me/en/articles/13135007-overview), [source repository](https://github.com/BasedHardware/omi), [MIT license](https://github.com/BasedHardware/omi/blob/main/LICENSE).
- [Oura Advisor support](https://support.ouraring.com/hc/en-us/articles/39512345699219-Oura-Advisor).
- [WHOOP personalized guidance, April 30, 2026](https://www.whoop.com/us/en/thelocker/new-ai-guidance-from-whoop/).
- [Limitless official site](https://www.limitless.ai/), [help site](https://help.limitless.ai/en/). Direct verification incomplete.

## A viable first segment to test

Start with glasses-wearing, privacy-conscious independent professionals who use an iPhone and Mac, have frequent conversations and commitments, and already care about wellbeing. Recruit 15-25 with recurrent real examples of forgotten commitments, fragmented personal context or manual journaling. This is a tractable initial segment hypothesis, not a claim that they have proven willingness to pay.

Recruit founders, consultants and independent practitioners outside highly regulated or employer-restricted recording contexts first. Avoid choosing a disease population before sensor accuracy and intended medical use are validated. Expand to Linux enthusiasts as an open-source cohort, without assuming they share the consumer customer’s workflow or willingness to pay.

Why this segment: conversations produce repeated memory/action demand; desktop execution can materially complete tasks; phone continuity matters while away from a computer; glasses can lower capture effort. They may value seeing how workload, routines and self-reported wellbeing coincide. That last connection must remain descriptive and appropriately qualified.

Possible alternative segments to interview in parallel: existing Theora users seeking better explanations of their data; people already using wearables but abandoning manual journals; caregivers seeking portable observations and reports. Compare pain frequency, recording acceptance, device wear tolerance, onboarding effort and payment, rather than selecting by enthusiasm for AI.

## Recurrent jobs and differentiating hypotheses

1. **Conversation to commitment:** At the end of a voluntarily recorded conversation, surface what was agreed, show the source, draft the next action, and remind the user when useful. It wins only if fewer commitments are lost and cleanup effort drops.
2. **Continue across devices:** A request begun on iPhone resumes on Mac or Linux with the same facts, task status and permissions; phone remains useful when the brain sleeps. It wins if users stop repeating themselves and tasks complete reliably.
3. **Understand a day:** Assemble a concise review from conversations, self-reports and quality-gated measurements. Say "heart rate was elevated during this period" rather than infer an emotion or causal explanation from HR alone. It wins if users make a useful choice with less manual journaling.
4. **Help carry out a chosen wellbeing routine:** Schedule a walk, protect a break, remember a stated goal, follow up after the user asks. It wins if completion improves without intrusive nudges.
5. **Own and inspect memory:** Show why the agent believes a fact, let the user correct/delete it, export their history and move between supported models or hosted/self-hosted deployments. It wins if continuity and trust improve enough to justify switching.

Candidate differentiation is the whole closed loop: observed context -> grounded memory -> user-approved action -> outcome -> updated context. Components exist elsewhere; the proposed advantage is coherent integration and actual reliability. No "first ever" or "learns everything" claim is supported.

## Validation plan and proposed decision gates

### Discovery, 2 weeks

Interview 20 target users using the last concrete incident, not hypothetical feature reactions. Ask what happened, consequence, existing workaround, frequency and what they would stop using. Collect consented workflow examples. Gate: at least 12 describe the same recurring job weekly and at least 8 commit to a four-week pilot. These are proposed internal thresholds.

### Concierge pilot, 4 weeks

With 15-25 people, test phone + existing brain before betting on new sensors. Provide conversation source links, 1-3 suggested commitments and one user-chosen routine. Compare a memory-only experience against memory plus action. Explicitly label simulated/manual assistance; avoid claiming automation that did not happen.

Measure weekly users obtaining at least three verified useful outcomes, repeat use after week 4, commitment extraction precision/recall on user-labeled examples, percentage of proposed actions accepted and completed, time saved, edit burden, capture failure rate, transcription coverage, and action failure/retry rate. Measure nuisance nudges, consent refusal and incorrect personal inference as negative outcomes. Suggested gates: >=90% commitment precision, no material unauthorized action, >=60% week-4 retained pilot users, and >=50% asking to keep using it at an honestly stated price. Small cohorts require uncertainty reporting; gates are not industry facts.

### Payment and sustained value, 6-8 weeks

Offer a real continuation purchase or refundable deposit at 2-3 transparently described prices. Separate hardware price, hosted inference cost and optional sync subscription. Track cohort retention, outcomes per active user, support minutes, gross contribution after model/audio/storage costs and active-device wear time. Do not substitute survey willingness or downloads for purchasing and retention.

### Incremental value of glasses and health

Within consenting users, compare phone capture with glasses capture and measure incremental coverage, convenience, battery/comfort and social acceptance. Compare a context-only daily review with one including current verified health signals. Ask whether the latter changes a useful decision, rather than whether it looks impressive. If biosensors add no repeated value, keep them optional while the hardware matures.

## Health measurement and product boundary

The current repository distinguishes the current health-temple glasses’ finished BP values from a proposed next-generation two-site PPG design. Raw synchronized optical measurements and continuous PTT are future work. App calibration and software quality scores do not themselves establish clinical validity. ECG APIs in a vendor SDK do not establish that the current device supplies a supported signal.

FDA's January 2026 general-wellness guidance concerns low-risk healthy-lifestyle functions, including certain software unrelated to disease diagnosis/treatment. Its safety communication specifically warns that unauthorized BP measurement devices, including wearable software features, do not qualify as general wellness merely because marketed that way. The January 2026 cuffless BP clinical-performance document is draft guidance, not final authorization. These sources support an explicit separation between a consumer wellbeing assistant and any BP medical-device program. [General wellness guidance](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/general-wellness-policy-low-risk-devices), [BP safety communication](https://www.fda.gov/medical-devices/safety-communications/do-not-use-unauthorized-devices-measuring-blood-pressure-fda-safety-communication), [cuffless BP draft guidance](https://www.fda.gov/regulatory-information/search-fda-guidance-documents/cuffless-non-invasive-blood-pressure-measuring-devices-clinical-performance-testing-and-evaluation).

Plan independent technical validation: source/time/unit provenance; worn-state and artifact handling; coverage and missingness; device-to-device disagreement; repeatability; representative skin tones, fit and activity conditions; reference comparisons appropriate to intended claims. The agent should distinguish measured values, firmware estimates, calibrated results, software inference and user statements. Broad "health intelligence" language cannot replace that distinction.

## Open-source promise and missing evidence

Useful commitments: clean-clone build; hardware-free replay/simulator path; documented sensor capability contracts; local export and deletion; user-controlled model credentials; demonstrable operation without a company cloud where promised; explicit cloud dependencies; redistribution rights for every vendor binary; signed releases and reproducible checks. Existing Theora vendor SDK/gitlink issues are release blockers for reproducibility, not proof that the entire product cannot be open source.

Important unresolved evidence: actual wear comfort/battery; attainable health-signal quality at temple; affordability and unit economics; permission to record in users' environments; first-time pairing success; real off-LAN reconnect and offline backlog reliability; demand among Linux users; region-specific medical/recording requirements; and whether users desire both general agency and health context in one product. Treat each as a research/test track with an owner and a decision gate.
