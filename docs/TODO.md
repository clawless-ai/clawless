TODOs:
- Add AI security code analysis gate to analyzer
- Ensure no naming/slug conflicts with existing skills during proposal
- Interactive approval mode does not work (only changes status)
- Give Agent knowledge about current DateTime
- Rethink status names and approval process flow (e.g. distinguish between installed, approved)
- Rethink actions e.g. distinguish betweeen approve,install,remove,delete
- Rethink list command (e.g. proposals vs. installed)
- Add testing loop for Implementer to test if the implemented skill works as intended and improve until it works. (How to do this without ending in an endless loop)? Check how OpenClaw handles this!!!

UNSOLVED ISSUES:
- Securely exchange API keys / secrets between user agent and admin without exposing it to external LLMs. For now, admin must set keys manually.
- Generated skill attempted to store API key in-line within the code. Define a standardized key storage schema which implementation service must consider for all skills.
- Implementer should avoid creating dummy code as fallback (e.g. instead return InfeasableError)

- Intent routing does not detect correct intent when asking "Wie wird das Wetter morgen?" E.g. it already worked that this lead to the cli stating it cannot do that but suggesting to LEARN it with user approval.
- Clear intents e.g. "Lerne XY" should not need additional approval. This also worked already but now doesnt work anymore.

IDEAS:
- Activity log for skill proposal / review flow
- Skills should externalize input parameters which reasoning can match against knowledge from memory (e.g. if user asks how the weather will be tomorrow and the weather skill offers a location input parameter and memory contains info where he lives, then these factors should be matched against and used to fulfill the user intent).

Open Questions:
- Implementer: What happens, when no solution was found (e.g. due to security constraints)?
- Weather app draft had empty dependencies (isn't it dependent on the cli?)
- Does the Reasoning skill already consider context from memory for intent rou
- How to prevent the implementer from trying to circumvent security rules (e.g. forbidden packages). Is this needed?
- Better understand how the TOOL system and Intent Routing system works!