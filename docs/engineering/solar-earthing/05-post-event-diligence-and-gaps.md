# 05 · Post-event diligence and industry gaps

> **Lightning honesty notice.** This repository shows *observed optical lightning* from EUMETSAT MTG LI L2-LFL. An optical flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress, or a second fault on an IT network. Nothing in this repository attributes a fault to lightning. Post-event inspection is a diligence recommendation, not a diagnosis.

This document sets out the owner's concerns about what should happen after a flash is observed near a solar plant. It also covers the gaps that the owner's research of 21 October 2026 found in standards, procurement, commissioning, training and incident data. These are concerns and hypotheses. Each substantive claim carries the evidence class assigned in the research [1]: **confirmed**, **reasonably inferred**, **disputed**, **unverified** or **unknown**. Where a point is the owner's own concern and the sources do not test it, it is marked as such.

## 1. Why post-event testing is good diligence

### 1.1 The data cannot show whether there was a ground strike

The satellite records optical flashes, including flashes inside clouds, and its dots are "not verified ground-impact locations" [5]. A dot near a site may be an in-cloud discharge, a distant ground stroke or a close one. An observed flash is therefore a reason to *look* and never a reason to *conclude*.

### 1.2 A strike does not need to hit the array to stress it

| Mechanism | What the sources say | Class |
|---|---|---|
| Magnetic induction in DC loops | A nearby discharge changes the flux through the area enclosed by the outgoing and return conductors, inducing V = −dΦ/dt. Loop area matters more than cable length [2][3]. | Standard physics; the application to PV is the owner's explanation |
| Earth-potential rise | For a 100 kA strike into a module in homogeneous soil of 200 Ωm, DEHN's model gives about 106 kV to remote earth at 30 m, 21 kV at 150 m and 11 kV at 300 m. Partial lightning currents flow "through the SPDs on the inverter AC side ... towards the transformer station" [4]. | Illustrative manufacturer model, not a site prediction |

Chapter 03 covers both mechanisms.

### 1.3 IT sites can hide damage

The owner's main concern is that, on an unearthed (IT) AC collection network, surge damage from any cause can become a first earth fault that nothing obvious reports.

- **The first fault is tolerated.** The healthy conductors rise from 462 V to 800 V to earth and stay there. Only capacitive current flows, and no overcurrent device operates [1]. *Confirmed mechanism.*
- **Detection rests on one device.** Detection depends on one insulation monitoring device (IMD) per galvanically connected network. The IMD duty is in BS 7671 411.6 and in none of the PV standards [1]. *Confirmed.* Whether A4:2026 places it at 411.6.3.1 is still open [1]. *Unverified.*
- **The fault can disappear from view.** Many inverters may trip together on residual current, pass their own insulation tests, reconnect and trip again, with none reporting an AC fault. Once they are off, the fault is "invisible unless a winding IMD exists" [1]. *Reasonably inferred, not measured.*
- **The inverter check runs only before start-up.** The inverter's own insulation check runs before connection. A first fault that appears during the day may not be seen until the next start-up [1]. *Reasonably inferred.*

A storm could therefore leave a site in a first-fault state that lasts for days. The research could not show that a shared air circuit-breaker (ACB) would clear a later second fault with normal margins [1]. *Reasonably inferred from stated assumptions.* Chapter 02 covers this. The research's forensic lesson is to "find the earlier tolerated event in the logs and the hardware" [1].

### 1.4 New and existing sites

- **New sites.** IMD thresholds and latching are set at commissioning, and the IMD's fault memory is off by default [1]. *Confirmed from the manufacturer manual, as reported in the research.* A post-event check also shows whether the alarm path works.
- **Existing sites.** One vendor advises lowering inverter insulation thresholds "to reduce nuisance trips in damp conditions". No document opened records who authorises such a change [1]. *Confirmed that the advice exists. The effect on any fleet is unknown.*

### 1.5 Pylons and overhead lines

The owner is concerned that sites near pylons or overhead lines may see more lightning-related stress, through tall earthed structures nearby and conductors that carry surges. None of the sources quantifies this. It is the owner's hypothesis and is **unverified**. It is offered as a reason to inspect these sites first, not as a finding.

## 2. Post-event checklist

Run this after an observed flash near a site, whether or not an alarm was raised. Each item yields a recorded value or a photograph. Work in this order so that evidence is kept before anything is disturbed.

| # | Check | Record | Basis |
|---|---|---|---|
| 1 | **Preserve data** | Export the inverter, SCADA and IMD logs at once. Save the flash record, because the feed keeps a seven-day rolling window [5]. | Event stores "can be lost at power-down" [1] |
| 2 | **Photograph first** | Switch handles, fuse holders, SPD indicators and connector states | Research forensic method [1] |
| 3 | **IMD logs and trend** | Readings before, during and after; alarms and whether they latched; the trend against the healthy baseline. Run a resistor functional test if the alarm path is in doubt. | Manufacturer instruction; the resistor test proves the IMD is on the right winding [1] |
| 4 | **RCMU logs** | Residual-current trips by inverter and by time; many inverters tripping together | Inferred signature of an AC-side first fault [1] |
| 5 | **Inverter insulation and thresholds** | Readings at the next start-up; settings read back against the design | Desensitisation "can mask developing faults" [1] |
| 6 | **SPD indicators and counters** | Flags, disconnectors and surge counters on DC type-2, AC type-1 and inverter-internal SPDs | Type-1/type-2 coordination [4]. Degraded MOVs can run away thermally [1] (inferred). |
| 7 | **Insulation-resistance (IR) tests** | IEC 62446-1 Category 1 per string, compared with the commissioning baseline as a relative change. AC feeders in the 1,000 V DC band (BS 7671 Table 64). | Disconnect inverters, SPDs and IMDs first (BS 7671 643.3.2) [1]. A wet test helps find intermittent faults [1]. |
| 8 | **Earthing and bonding continuity** | Frames, structures, inverter PE and the transformer earth; condition of bonding jumpers | BS 7671 643.2. Bonding resistance changes the second-fault current [1]. |
| 9 | **Connectors and thermography** | Category 2 thermography on a sample; cross-mated, half-inserted or damaged connectors | An IMD may not detect a series high-resistance joint or a series arc [1][2] |
| 10 | **Weather at the time** | Rain, condensation, wind, temperature; flash times and distances | In the research's illustration, 20 wet arrays at 50 kΩ each read as 2.5 kΩ [1] (inferred). See chapter 04. |
| 11 | **Safe isolation** | Manufacturer procedure; no connector pulled under load | Manufacturer instructions [1] |

A finding from this checklist is a defect to investigate, not a cause. The research lists "defaulting to lightning" among the common misattributions in fault investigation [1].

## 3. Other fault sources

| Source | Note | Class |
|---|---|---|
| HV earth fault transferred as overvoltage | IEC 60364-4-44 allows U + 1,200 V for up to 5 s on IT, which is 2,000 V on an 800 V network. DEHN argues that with an unearthed LV star and meshed earthing this "can, under certain conditions, be ignored" [1][4]. | Rule confirmed; DEHN's view is a manufacturer's |
| MV switching transients | Vacuum-breaker reignition [1] | Confirmed for wind, not demonstrated for PV |
| Restriking first fault | 6 to 8 × phase voltage (IEEE 142/242), 2.8 to 3.7 kV at 800 V [1] | Derived figures |
| Second fault on IT | 5 to 10 kA between two feeders in the research's models [1] | Reasonably inferred |
| Moisture ingress | Isolators, junction boxes, combiners [1][2] | Confirmed for isolators; inferred for connectors |
| Insulation damage and ageing | Chafing, pinched ties, rodents, UV [1] | Confirmed cases; inferred mechanism |
| Connectors and terminations | Cross-mating, rising resistance, series arcs [1] | Rule confirmed; field evidence mostly held by manufacturers and insurers |
| Healthy wet-weather leakage | Nuisance trips and low IMD readings with no insulation fault [2] | Physics; magnitudes illustrative |

## 4. Gaps the research identifies

The research finds the blind spot to be a gap in "standards scope, procurement language, inspection checklists, training and data taxonomy". It confirms that gap at the documentation level and treats it as **reasonably inferred** to be systemic, from a small sample. The safety outcome is **not demonstrated**: no investigation, insurer note or dataset attributes a fire, injury or transformer failure to this mechanism [1].

| Area | Finding | Class |
|---|---|---|
| Standards scope | IEC 62548-1 excludes "the AC or DC distribution network". BS 7671 Section 712 does not address the inverter output network. IEC 63112 acknowledges an unearthed AC side but is an equipment standard [1]. | Confirmed |
| Standards activity | IEC 60364-7-712 Ed. 3 (2025) claims to add requirements for PV generation plants; its text was not opened [1] | Unknown |
| Procurement | Three public documents leave the arrangement to "design" and require no IMD. None is a utility-scale string-inverter specification [1]. | Reasonably inferred; small sample |
| Commissioning checklists | The guides opened include no IT earthing study, double-fault loop check or IMD test, and no check of installed devices against the single-line diagram [1] | Confirmed for the documents opened |
| Alarm ownership | No PV document opened assigns responsibility for responding to an IMD alarm [1] | Confirmed for the documents opened |
| Training | One UK Level 3 PV syllabus does not cover IT systems or insulation monitoring. No UK qualification covers utility-scale LV collection commissioning [1]. | One syllabus confirmed; others by title |
| Incident taxonomy | The UK fire dataset is dominated by rooftop systems. kWh Analytics has no earth-fault category. IEA PVPS calls logbooks "not harmonised". Farm incidents often go unreported [1]. | Confirmed |
| Lightning attribution | No dataset opened links lightning to AC earth-fault outcomes on string-inverter sites, so an observed flash risks becoming a convenient label | Owner's concern, consistent with [1] |

To settle the question, the research calls for an as-built survey of 50 or more plants. It also calls for fleet alarm telemetry split between AC and DC, a ticket category for AC earth faults, insurer claims coding and staged cross-feeder tests [1]. Consistent post-event records would add to that evidence.

## 5. Using this repository's data

| May | May not |
|---|---|
| Trigger a log review and this checklist | Claim or imply that an asset was struck |
| Set the window for exporting logs | Attribute any fault, trip or failure to lightning |
| Give context alongside alarm timing and weather | Serve as evidence of a strike in an insurance, warranty or contract claim |
| Help decide which sites to inspect first | Serve as a site risk assessment; it is "not a risk assessment" [5] |
| | Show that no lightning occurred; an empty feed "does not mean no lightning" [5] |

The repository targets, analyses and names no site [5].

## References

1. Owner's research, *Earthing, inverter topology and protection in utility-scale solar*, 21 October 2026 (anonymised; evidence classes as assigned there).
2. Owner's notes, *Wet weather, solar panel capacitance and lightning*, 22 October 2026.
3. Owner's notes, *Solar farms: lightning, cable loops and IT earthing*.
4. DEHN SE, White Paper WPX051/EN/0422, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*, 2022. https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf
5. Ventusltd/lightning repository README: source and meaning of the data (EUMETSAT MTG LI L2-LFL, collection EO:EUM:DAT:0691).

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*
