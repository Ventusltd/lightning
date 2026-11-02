# IT AC collection networks: first fault, second fault and monitoring

> **Lightning notice.** The lightning repo shows *observed optical lightning* from EUMETSAT MTG LI L2-LFL. An observed flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress or a second fault on an IT network. Nothing in this repo attributes a fault to lightning, and post-event inspection is a diligence recommendation, not a diagnosis.

This note sets out the owner's (Vikram, Ventus Ltd) concerns about unearthed (IT) LV AC collection networks behind transformerless string inverters. It draws on the owner's research of 21 October 2026 [1], which anonymises all projects, and on DEHN WPX051 [2]. Each substantive claim carries the research's evidence class: **confirmed**, **reasonably inferred**, **disputed**, **unverified** or **unknown**. Figures from the research's fault models for an anonymised "reference block" are **illustrative model outputs**, not a protection study.

## 1. The concern

On many 1,500 V string-inverter plants, 8 to 14 inverters share one 800 V winding across hundreds of metres of cable with no earthed star point (**confirmed** as a described architecture) [1]. The owner's concern has four parts (**reasonably inferred**) [1]:

- A first earth fault trips nothing.
- The healthy phases then stay at line voltage to earth.
- Only one insulation monitoring device (IMD) can see the fault.
- A second fault depends on a per-feeder calculation that is often not done.

This is a hypothesis about an under-specified interface. No investigation or dataset found attributes a fire, injury or transformer failure to it, so the safety outcome is **unknown** [1].

## 2. Manufacturer positions, as sourced

IT operation is a system-design choice, not a necessity of the transformerless topology (**confirmed**) [1].

| Maker | Position | Class |
|---|---|---|
| Sungrow (SG285–350HX) | "Make sure the inverter is applied to an IT system before enabling the anti-PID function". With anti-PID on, "neutral point grounding is prohibited". LV winding, cables and secondary equipment must withstand ≥ 906 V to earth. Up to 15 inverters per winding. | Confirmed |
| Huawei (2024–2026 manuals) | Supports "the IT earthing system in PV-only scenarios"; TT and TN "not recommended". The 2020 manual also accepted an earthed star point; the reason for the change is not stated. | Confirmed; reason unknown |
| GoodWe | States IT. This conflicts with its four- and five-wire terminal text. | Confirmed; disputed internally |
| SMA | Requires an earthed star point for Sunny Highpower; IT "not permitted" for Sunny Tripower X and CORE2. | Confirmed |

**One device per inverter.** Every maker requires a protective device per inverter, but the device varies: a switch (Huawei), a switch with a breaker or fuse (GoodWe), or a rated breaker (Sungrow, Growatt). No maker explains why sharing is prohibited (wording **confirmed**; the reason is **unknown**) [1].

**DEHN** says the inverter-to-transformer link is "in most cases" a "three-phase IT system" [2].

**The 906 V figure.** The research reconstructs it as √(750² + 508²), a 750 V anti-PID DC offset combined with the phase voltage at 880 V (**reasonably inferred**; Sungrow publishes no derivation) [1]. Paper 01 covers the DC side.

## 3. First fault: voltages

After a solid phase-to-earth fault on an IT network, the healthy phases rise to line voltage to earth. The principle is **confirmed**; the figures are **derived** from U₀ = U/√3 [1].

| Condition (800 V network) | Conductor to earth |
|---|---|
| Healthy, r.m.s. | 462 V |
| After a solid first fault | 800 V |
| Same at +10 % supply tolerance | 880 V |
| IEC 60364-4-44 TOV, ≤ 5 s (U + 1,200 V on IT) | 2,000 V |
| Restriking fault, 6–8 × phase voltage (IEEE 142/242) | 2.8–3.7 kV |
| Anti-PID 750 V DC offset with AC superimposed | 881 V r.m.s., 1,403 V peak |

On a 1,000 V network the healthy phases rise from 577 V to 1,000 V [1].

**Temporary overvoltage.** DEHN gives the same 2,000 V figure and notes that standard SPDs "are not usually designed for these voltages". It also argues that with intermeshed earthing and an unearthed LV neutral, these TOVs "can, under certain conditions, be ignored" [2]. The IEC table itself is **unverified** [1].

**Restrike.** Whether these figures apply with inverter filters and buried cable is **unknown** [1].

**Duration.** IEC 60364-4-41 asks for elimination "with the shortest practicable delay" but sets no time. Without an IMD, durations of days to months are credible (**reasonably inferred**; no field data were found) [1].

## 4. First fault: currents

With no metallic return, the first-fault current is the network's capacitive charging current (**confirmed** mechanism) [1]. Through transformerless bridges, that capacitance includes the modules. The research's illustrative model of the 800 V block gives:

| State | First-fault current | Touch voltage to remote earth (0.5 Ω) |
|---|---|---|
| Inverters off | ~1.3 A | < 1 V |
| On, arrays dry | ~3.7 A | — |
| On, front glass wet | ~76 A | ~38 V |
| On, both faces wet | ~149 A | ~75 V, above the 50 V of 411.6.2 |

These are **illustrative, reasonably inferred** results, not measurements. The array capacitance per inverter was taken as 1.2 µF dry, 36.5 µF front wet and 72.7 µF both wet, from SMA's 60–110 nF/kWp at 475 kWp [1]. Paper 04 covers wet-weather capacitance [3].

The research infers the following sequence (**reasonably inferred**, not measured) [1]:

1. An AC first fault drives about 0.145 A per µF of array capacitance through each inverter (I = ωCV at 50 Hz and 462 V).
2. Each inverter's RCMU sees 5–10 A and trips within 40–300 ms. The inverters trip in waves; none identifies an AC fault.
3. Each inverter passes its own insulation test, reconnects and trips again.
4. Once all inverters are off, the fault is invisible unless a winding IMD exists.

A wet-day AC fault can therefore take out the whole winding's generation, the continuity IT was meant to preserve.

## 5. Detection relies on a single IMD

While the plant generates, the only device watching the AC collection network is an IMD on the LV winding (**reasonably inferred**) [1]. The inverter tests its array only before start-up, and its RCMU sees only current through its own terminals.

- **Only one IMD per system.** Bender says "Only one ISOMETER may be connected in a galvanically connected system". Schneider gives the same rule (**confirmed**) [1].
- **Thresholds.** General IT practice of 100 Ω/V gives 80 kΩ at 800 V. PV IMDs ship at 10 kΩ pre-warning and 1 kΩ alarm, and IEC 62548-1 accepts 1 kΩ above 500 kW. One unit cannot read above 50 kΩ at 2,000 µF (**confirmed**). PV networks therefore run about two orders of magnitude below other IT practice (**reasonably inferred**) [1].
- **Aggregation.** The IMD sees all arrays in parallel. Twenty wet arrays at 50 kΩ each read as 2.5 kΩ, which looks the same as one dangerous array at 2 kΩ when dry. Recurring morning alarms get acknowledged rather than investigated (**reasonably inferred**) [1].
- **Response time.** It is published only at 1 µF (10 s typical). At 500–4,000 µF it is **unverified** [1].
- **Latching.** One PV IMD's fault memory is off by default. Making the alarm persist while the fault persists is therefore a commissioning setting (**confirmed** default) [1].
- **No requirement.** None of the commissioning guides, checklists or procurement documents opened requires, records or tests a winding IMD. No PV document says who responds to the alarm (**reasonably inferred** from a small, consistently silent sample) [1].

## 6. BS 7671 411.6

BS 7671:2018+A4:2026, published 15 April 2026, is the only current edition [1]. The research did not open the printed text. The substance below is **confirmed** through Bender's and Schneider's statements of the matching IEC 60364-4-41 clauses [1].

| Regulation | Substance | Class |
|---|---|---|
| 411.6.2 | First fault: R_A × I_d ≤ 50 V | Confirmed |
| 411.6.3.1 | An IMD shall be provided | Substance confirmed; **number disputed** (the BSI 2018 notes say "deleted", which the research reads as a renumbering). Check against printed A4 before quoting. |
| 411.6.4 | Second fault, collectively earthed: 2 I_a Z_s ≤ U, with U = 800 V | Confirmed |
| 411.3.2.3 | 5 s for a 254 A distribution circuit | Confirmed |
| 411.6.5 | RCD non-operating current ≥ first-fault current | Confirmed |

That A4 leaves these rules unchanged is **reasonably inferred** from three change summaries [1].

## 7. The second fault, per feeder

A second fault on another phase in a different feeder closes a line-to-line loop through the protective conductors and the structure. The research's two independent models conclude (**reasonably inferred** from stated assumptions) [1]:

- **Fault level.** A bolted second fault between two 100–150 m feeders gives **5–10 kA**, or **1.3–2.6 ×** a shared 4,000 A ACB's rating.
- **Path resistance.** About 0.1 Ω of joints or a corroded bond brings the current down to the ACB rating, where it looks like load. At 0.5 Ω it falls to about 1.4 kA.
- **Bonding against transformer impedance.** Bonding resistance matters far more than transformer impedance. Moving from 0 to 0.5 Ω cuts the 150 m current from 7.4 to 1.3 kA. Changing transformer impedance from 6 % to 12 % changes it by about 4 %.

The section 16 model gives these results (illustrative: 400 mm² aluminium line, 200 mm² aluminium PE, simplified device curves) [1]:

| Feeder length | Second-fault current | 400 A MCCB per feeder | Shared 4,000 A ACB |
|---|---|---|---|
| 50 m | 8.8 kA | 20 ms | ~74 s |
| 100 m | 6.6 kA | 20 ms | ~134 s |
| 150 m | 5.2 kA | 20 ms | ~213 s |
| 200 m | 4.3 kA | 20 ms | ~310 s |
| 300 m | 3.2 kA | thermal region | never |

**Shared ACB.** With normal margins, a shared ACB cannot be shown to do any of the following (**reasonably inferred**) [1]:

- meet 2 I_a Z_s ≤ U within 5 s at the far end of 150 m feeders
- clear resistive faults through the structure
- protect a 185 mm² PE at a 0.3 s short-time delay.

**Per-feeder device.** A 315–400 A fuse or MCCB at the busbar or combiner end clears the same faults in under 0.1 s, with a 2–7 × margin. A device at the inverter end cannot protect the cable. For a cable fault it sees only its own inverter's current, at most about 1.4 × I_max [1].

**Weakest element.** A 16 mm² copper bonding jumper carrying 4 kA fails in about 0.33 s [1]. A resistive second fault of 0.5–4 kA sits below the ACB pickup and dissipates kilowatts to a megawatt at the joints. In the research's words, this is the difference between "a clean trip or a slow fire" [1].

**Not a verdict on any drawing.** A drawing without an MCCB at the inverter is not by itself a non-compliance. The fault study, the device data at 800 V and the maker's written acceptance decide that. The block's transformer and breaker certificates could soften these conclusions, which remains **unknown** until checked [1].

## 8. Cable insulation: the IEC 60502-1 correction

The owner's earlier work said a 0.6/1 kV cable fell 200 V short on an 800 V IT network. **That was wrong** (**confirmed** correction) [1].

- IEC 60502-1 Table 1 keeps U₀ = 0.6 kV for Category C at Um 1.2 kV. The step-up to a higher U₀ begins only at Um 3.6 kV.
- An 800 V IT network at 800–920 V to earth is therefore within a 0.6/1 kV cable's envelope.
- Category B allows earth faults of up to 8 h per occasion and 125 h a year.

**The DC offset is the open question.** With anti-PID or a DC pole fault, the AC conductors sit at 881–906 V r.m.s. and about 1,403–1,469 V peak to earth, which no LV AC cable standard contemplates (**reasonably inferred**) [1]. Any case for 1,000 V-to-earth insulation rests on this offset and on the cable maker's agreement, not on the earth-fault category.

**Ageing, not breakdown.** An intact cable does not puncture at these levels; the concern is ageing at terminations, joints and wet insulation, and no LV study was found (**unknown**). Whether HD 603 or BS 7889 limit 0.6/1 kV cables to Categories A and B is **unverified** [1].

## References

1. Vikram (Ventus Ltd), *Earthing, inverter topology and protection in utility-scale solar*, independent research, 21 October 2026 (projects anonymised). The manufacturer, Bender, Schneider, BS 7671 and IEC material are cited as recorded in its source register.
2. DEHN SE, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*, White Paper WPX051/EN/0422, 2022. https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf
3. Vikram (Ventus Ltd), *Wet weather, solar panel capacitance and lightning*, notes, 22 October 2026.

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*
