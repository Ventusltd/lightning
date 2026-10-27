# Floating PV DC and IT earthing: the owner's engineering concerns

> **Lightning honesty notice.** This repository shows *observed optical lightning* from EUMETSAT MTG LI L2-LFL. An optical flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress, or a second fault on an IT network. Nothing in this repository attributes a fault to lightning. Post-event inspection is a diligence recommendation, not a diagnosis.

This folder records the engineering concerns of the repository owner, Vikram (Ventus Ltd), about earthing and protection on utility-scale solar plants. The plants in question use transformerless string inverters with a floating DC side and an unearthed (IT) AC collection network. The concerns are presented as hypotheses with their evidence. They are not findings about any plant. The main source is the owner's independent research of 21 October 2026 [1], supported by the owner's notes [2][3] and a DEHN white paper [4]. No project, site, client or person other than the owner is named.

## The concerns in brief

1. **One unearthed system from module to transformer.** A block of transformerless string inverters forms a single, galvanically connected, unearthed system that runs from the module glass to the transformer's LV winding. The DC side cannot be earthed. IT on the AC side is a manufacturer's design choice, not a property of the topology. *Confirmed* [1].
2. **The first fault is hard to see.** On a first earth fault the healthy conductors rise from 462 V to 800 V to earth, yet only capacitive current flows. Detection rests on a single insulation monitoring device (IMD), whose duty sits in BS 7671 411.6 and in none of the PV standards. *The mechanism is confirmed; the practice gap is reasonably inferred* [1].
3. **The second fault is not shown to clear.** The research models a second fault between two feeders at 5 to 10 kA. A shared ACB cannot be shown to clear it with normal margins, whereas a fuse or MCCB on each feeder clears the same cases quickly. *Reasonably inferred from stated assumptions* [1].
4. **Lightning and wet weather add stress that IT earthing does not prevent.** Nearby lightning can induce voltage in large DC loops (V = −dΦ/dt) and drive partial currents through the AC-side SPDs towards the transformer [3][4]. Wet arrays raise capacitance and leakage (I = C·dV/dt) [2]. IT earthing prevents neither mechanism [3]. The wet-weather figures are *illustrative*.
5. **Standards, procurement, commissioning, training and incident data all have gaps.** The research confirms these gaps at the level of documentation. The safety outcome is *not demonstrated*: no investigation, insurer note or dataset attributes harm to this mechanism, and none is structured to detect it [1].

## Documents

| No. | Document | Scope |
|---|---|---|
| 01 | [The floating PV DC side](01-floating-pv-dc.md) | Transformerless inverters; why the DC side cannot be earthed; functional earthing and anti-PID; DC offset and common-mode voltage; insulation monitoring and what it cannot detect |
| 02 | [IT AC collection networks](02-it-ac-collection.md) | Manufacturer positions; first-fault voltages and currents; reliance on one IMD; BS 7671 411.6; second-fault calculation per feeder; cable insulation |
| 03 | [Lightning coupling](03-lightning-coupling.md) | Magnetic induction in string loops; earth-potential rise and partial lightning currents; type-1 vs type-2 SPDs; pylons and overhead lines; why IT earthing prevents neither mechanism |
| 04 | [Wet-weather leakage](04-wet-weather-leakage.md) | Dry and wet capacitance (illustrative); I = C·dV/dt and d(CV)/dt; RCMU nuisance trips; IMD settings; capacitive leakage vs genuine insulation faults |
| 05 | [Post-event diligence and gaps](05-post-event-diligence-and-gaps.md) | Why testing after an observed flash is good diligence; a post-event checklist; other fault sources; gaps in standards and practice; how this repository's data may and may not be used |

The documents can be read in any order. Start with 01 and 02 for the earthing argument, or go straight to 05 for the practical checklist.

## Evidence classes

Every substantive claim carries one of the five classes used in the owner's research [1]. The documents keep the class the research assigned and never upgrade it.

| Class | Meaning |
|---|---|
| **Confirmed** | Taken from a source the research opened and read |
| **Reasonably inferred** | Follows from confirmed facts or stated assumptions, but has not been measured or published as such |
| **Disputed** | Sources disagree, and the disagreement is recorded |
| **Unverified** | Reported or plausible, but the research could not check it against a primary source |
| **Unknown** | No source found either way |

Two further labels appear. **Illustrative** marks figures that show scale under stated assumptions, such as the roughly ×2 wet/dry capacitance examples, which are "not a universal multiplier" [2]. **Owner's concern** marks a hypothesis the sources do not test. **DEHN states** marks a claim taken from the DEHN white paper WPX051 rather than from the owner's research; it carries DEHN's authority, not an independent confirmation.

## How to use this folder

- **Read it as engineering discussion.** It sets out risks and the questions to ask of a design. It does not judge whether any plant complies.
- **Treat manufacturer statements as manufacturer statements.** Manufacturers and standards are named only where the sources name them. Quotations are kept short, and third-party text keeps its owners' terms (see NOTICE).
- **Do not use the lightning data as evidence of a strike.** The map shows optical flashes and "not verified ground-impact locations" [5]. It may prompt a check, but it does not explain any fault.

## References

1. Owner's research, *Earthing, inverter topology and protection in utility-scale solar*, 21 October 2026 (anonymised; five evidence classes).
2. Owner's notes, *Wet weather, solar panel capacitance and lightning*, 22 October 2026.
3. Owner's notes, *Solar farms: lightning, cable loops and IT earthing*.
4. DEHN SE, White Paper WPX051/EN/0422, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*, 2022. https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf
5. Ventusltd/lightning repository README: source and meaning of the data (EUMETSAT MTG LI L2-LFL, collection EO:EUM:DAT:0691).

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*
