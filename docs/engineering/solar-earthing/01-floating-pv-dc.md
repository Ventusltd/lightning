# 01 · The floating PV DC side behind transformerless string inverters

> **Lightning notice.** The lightning repo shows *observed optical lightning* from EUMETSAT MTG LI L2-LFL. An observed flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress, or a second fault on an IT network. Nothing in this repo attributes a fault to lightning. Where post-event inspection is suggested, it is a diligence recommendation, not a diagnosis.

These are the owner's concerns and hypotheses, set out with the evidence behind them. Each substantive claim carries the evidence class (confirmed, inferred, disputed, unverified, unknown) assigned in the owner's research of 21 October 2026 [1]. No project, site or client is named.

## 1. The concern in one paragraph

A block built on transformerless string inverters is one galvanically connected, unearthed system from the module glass to the LV winding of the step-up transformer **[confirmed]** [1]. Its DC side cannot be earthed, anti-PID can put a DC offset on every AC conductor, and in operation one insulation monitoring device (IMD) is the only watch on the whole system, a device blind to series faults and arcs. The owner's concern is that PV standards, procurement and commissioning documents do not cover this well.

## 2. One galvanic system, from module to LV winding

In a transformerless inverter the PV negative is the DC-bus negative. SMA says part of the AC voltage reaches the array, so "the complete PV array oscillates with an alternating voltage in relation to its environment" **[confirmed]** [1, 4]. In three-phase units the low-frequency swing is "largely suppressed" but is not zero **[confirmed]** [4].

The research reads the circuit this way **[inferred]** [1]:

- The array sits near −V_dc/2 plus a common-mode ripple relative to the AC star point.
- Filter Y-capacitors, array capacitance and leakage resistance decide where the floating DC side settles.
- When 8 to 14 inverters share one winding, every array behind every inverter and every LV conductor become **one IT system**. Their insulation resistances and capacitances combine in parallel. Central plants gave each inverter its own LV winding; string plants share one across hundreds of metres of 800 V cable, so the galvanic separation between arrays has gone **[confirmed]** [1].

## 3. Why the DC side cannot be earthed

SMA states that in a transformerless inverter a ground fault during feed-in drives grid current through the bridge that "can exceed 1 kA", and that disconnection "cannot occur quickly enough to prevent damage", which is excluded from warranty **[confirmed]** [1, 5]. Sungrow's HX manual says that neither PV pole can be grounded, or "the inverter will not operate normally" **[confirmed]** [1, 6].

The mechanism, as the research reconstructs it **[inferred]** [1]: with an earthed LV star point, an earth on DC− closes a loop from the transformer neutral through earth, DC−, the lower free-wheeling diodes or IGBTs, the phase conductor and the winding. That is a line-to-earth short through the bridge, limited only by source impedance; the DC-link capacitors do not block it. DC+ gives the mirror loop.

On an IT winding the same single earth has no metallic return. It produces only capacitive and monitoring currents, and it turns the floating system into a referenced one: a **first fault** in IT terms **[inferred]** [1]. Why Sungrow and Huawei still forbid pole earthing on IT is not stated; the research infers that their monitoring assumes a floating array, that a second earth elsewhere would close a loop through two bridges, and that a deliberate earth defeats a plant-level IMD **[inferred]** [1].

## 4. IT is a design choice, not a law of physics

The topology does not force IT on the AC side **[confirmed]** [1]:

| Maker (as sourced in [1]) | AC earthing position |
|---|---|
| SMA Sunny Highpower PEAK3 | Transformerless, 1,500 V DC, yet "the neutral point must be grounded" |
| SMA Sunny Tripower X, CORE2 | TN or TT only; "Operation … in IT or Delta IT grids is not permitted" |
| Sungrow HX | IT before anti-PID is enabled; star-point earthing "prohibited" with anti-PID; up to 15 inverters per LV winding |
| Huawei (2024–2026 manuals) [7] | "the IT earthing system in PV-only scenarios"; TT and TN "not recommended" |
| Huawei (2020 manual) | Also accepted "neutral point grounding of the step-up transformer"; later withdrawn, reason **unknown** |
| GoodWe HT | States IT, but its terminals and RCD advice read like TN template text **[disputed]** |

IT allows a daytime anti-PID offset, limits leakage return paths when many inverters share a winding, and rides through a first fault **[confirmed facts; causal link inferred]** [1].

## 5. Functional earthing and anti-PID

The research distinguishes *functional* earthing (a connection made so that equipment works, such as an earthed array pole or an anti-PID offset) from system earthing, protective earthing and bonding. An IT system keeps protective earthing and bonding; only its source is unearthed [1].

DEHN describes potential-induced degradation (PID) as affecting crystalline cells held at a high negative potential relative to the earthed frame [3]. Anti-PID schemes shift that potential, and they do it in different ways:

- **DEHN** describes units that "feed in DC voltage between the phases and earth on the AC side", up to 50 % of string voltage (750 V DC at 1,500 V), and tested its SPDs against 1,450 V superimposed peaks [3].
- **Sungrow** raises the array negative "to close to the ground potential" during the day. Its manual requires the LV winding, the AC cables and the secondary equipment to withstand at least **906 V** to earth when anti-PID is enabled **[confirmed]** [1, 6].
- **Huawei** places the function in a separate SmartPID2000 unit, which injects 50–800 V DC to ground onto the AC busbar of a network "without neutral line" **[confirmed]**. Whether it runs by day or by night is **[inferred]** [1, 7].
- **Solis** and **SMA** use night-only schemes **[confirmed]** [1].

Central plants could functionally earth a pole through a GFDI, with a documented blind spot: a fault on the earthed pole bypasses the GFDI **[confirmed]** [1]. Transformerless string arrays are floating only; instead, anti-PID shifts each array's reference to earth **[confirmed]** [1].

## 6. The DC offset to earth and common-mode voltage

Sungrow does not define 906 V. The research reconstructs it as DC offset plus phase voltage, r.m.s. **[inferred; consistent to a volt; Sungrow has published no derivation]** [1]:

V_rms = √(V_DC² + V_LN²) = √(750² + 508²) ≈ 906 V

Here V_DC = 750 V (half of 1,500 V) and V_LN = 508 V (the phase voltage at 880 V, which is 800 V + 10 %). The peak is V_DC + √2·V_LN ≈ 1,468 V. That sits near DEHN's 1,450 V test peaks [3]; a reported 1,500 V figure in a Polish edition of the manual is **[unverified]** [1].

The research's derived voltages for an 800 V network [1]:

| Condition | Conductor to earth |
|---|---|
| Healthy, r.m.s. | 462 V |
| After a solid AC first fault | 800 V (880 V at +10 %) |
| 750 V DC offset with AC superimposed, r.m.s. | 881 V (906 V at 880 V) |
| Same, peak | 1,403 V (about 1,470 V at 880 V) |

The IT first-fault principle is **confirmed**; the figures are derived arithmetic. SMA's central-inverter manuals confirm the general effect: with the AC side ungrounded there are "up to 1000 V" between conductor and ground **[confirmed]** [1]. The owner's concern is that LV AC equipment standards do not contemplate this offset, so windings, cables, CTs, VTs and PE-referenced instruments need their makers' written acceptance **[inferred]** [1]; cable category is covered in document 02.

Capacitive leakage follows I = C·dV/dt, or I = d(CV)/dt when capacitance itself changes, as it does when modules wet [2]. SMA gives 60–110 nF/kW for crystalline arrays **[confirmed]** [1, 4]; wet weather is covered in document 04.

## 7. Insulation monitoring: what watches the floating system

While the plant generates, the only device watching the whole system is an IMD on the LV winding **[inferred from device principles and Bender's description]** [1]:

| Device [10] | What it sees while generating | What it does not see |
|---|---|---|
| Inverter pre-start insulation test (IEC 62109-2 4.8.2) | Nothing. It runs only with the grid relay open | The AC feeder, and anything after the relay closes |
| Inverter RCMU (IEC 62109-2 4.8.3) | Net residual current through its own terminals | Faults with no return through that inverter; a resistive DC fault on a floating network returns only through capacitance and may stay below 300 mA |
| IMD on the winding (IEC 61557-8) | Any resistive path to earth, AC or DC, as one aggregate | Where the fault is; fast events; small degradation in one array masked by the rest |

The research identifies three practical problems:

1. **Only one IMD per system.** Bender: "Only one ISOMETER may be connected in a galvanically connected system". Schneider: "no more than one IMD injecting into the system" **[confirmed]** [1, 9]. No IEC text opened states it, and no tested IMD–inverter coordination is documented **[unknown]** [1].
2. **Aggregation.** Twenty wet arrays at 50 kΩ each read as 2.5 kΩ, which cannot be told apart from one dangerous 2 kΩ array on a dry day. Factory defaults on PV units are 10 kΩ pre-warning and 1 kΩ alarm. This is how recurring morning alarms may come to be ignored or thresholds raised **[inferred, not measured]** [1].
3. **Range and response.** At 2,000 µF the isoPV1685RTU cannot measure above 50 kΩ. Response times are published only at 1 µF (10 s typical). Latching is off by default on the isoPV1685DP, so the persisting signal BS 7671 411.6.3.1 requires is a commissioning setting **[confirmed]** [1]; that clause number in the printed A4:2026 is **[unverified]** [1].

An AC-side first fault drives about 0.145 A per µF of array capacitance through each inverter (ωCV at 50 Hz, 462 V): the likely result is waves of residual-current trips, with no inverter reporting an AC fault **[inferred]** [1].

## 8. What an IMD cannot detect

An IMD measures resistance to earth. By design it is blind to faults that never involve earth:

- **Series faults.** A loose termination, a half-mated or cross-mated connector, or a relaxed fuse-holder clip raises resistance *in series* with the circuit. The owner's notes state that an IMD "may not detect a purely series high-resistance connector or series arc" [2]. Loose terminations are the canonical series-arc case **[confirmed]** [1].
- **Series arcs.** "The system seems to continue running normally" **[confirmed]** [1, 11]. Detection is left to arc-fault detection: UL 1699B, or IEC 63027:2023, which "does not cover the detection of parallel arcs" **[confirmed scope]**. Interrupting at the inverter removes only series-arc current, so a parallel or earth arc fed by the string continues **[inferred consequence]** [1].
- **Array-internal faults at night**, and slow events. Sandia records that insulation measurement "is very slow … due to array capacitance", and that at night bypass diodes can hide array-internal faults that "could subsist indefinitely" **[confirmed]** [1, 8].

The lesson the research draws from documented grounded-array fires (2009, 2011) is that a tolerated first fault (an alarm below threshold, a small leakage, a half-mated connector) can turn a later ordinary fault into a fire **[inferred]** [1].

## 9. What the owner is asking for

- A stated earthing arrangement per LV winding, with the anti-PID mode and conductor-to-earth voltages (DC, r.m.s., peak) declared to every supplier.
- One IMD per galvanically separate system, rated for the offset, with justified and latched thresholds, a resistor test at commissioning and a named alarm owner.
- Arc-fault detection, matched connectors and thermography, because an IMD cannot find series faults.
- Written answers from inverter makers on what 906 V means and how the inverter coordinates with a central IMD [1].

## References

1. Owner's independent research, *Earthing, inverter topology and protection in utility-scale solar*, 21 October 2026 (anonymised; internal).
2. Owner's notes, *Wet weather, solar panel capacitance and lightning*, 22 October 2026.
3. DEHN SE, White Paper WPX051/EN/0422, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*, 2022. https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf
4. SMA, Technical Information, *Leakage Current* (as cited in [1]).
5. SMA, Technical Information, *Insulation Error* (as cited in [1]).
6. Sungrow, SG285/320/333/350HX user manual, Ver24 (September 2025) and Ver28 (September 2026) (as cited in [1]).
7. Huawei, SUN2000 manuals 2020–2026; SmartPID2000 datasheets (as cited in [1]).
8. Sandia National Laboratories, SAND2016-0145R, 2016 (as cited in [1]).
9. Bender, isoPV1685 manuals and PV brochure; Schneider Electric, IM10/IM20 guide (as cited in [1]).
10. IEC 61557-8:2014; IEC 62109-2:2011; IEC 62548-1:2023+AMD1:2025; IEC 63112:2021; IEC 63027:2023; BS 7671:2018+A4:2026 (clause content as cited in [1]; printed texts not opened).
11. TÜV Rheinland / Fraunhofer ISE, PV fire-risk guideline (as cited in [1]).

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*
