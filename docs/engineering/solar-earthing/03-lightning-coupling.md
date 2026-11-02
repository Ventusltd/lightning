# 03 · Lightning coupling into floating PV DC and IT AC collection networks

> **Lightning honesty notice.** The lightning repo shows *observed optical* lightning from EUMETSAT MTG LI L2-LFL. An observed flash is not necessarily a ground strike, and it is not evidence that any asset was hit. Fault currents, surges and insulation damage can also come from grid transients and switching surges, equipment failure, insulation degradation, moisture ingress, or a second fault on an IT network. Nothing in this repo attributes a fault to lightning. Post-event inspection is a diligence recommendation, not a diagnosis.

This document sets out the owner's concerns about how lightning couples into a solar block built on transformerless string inverters with an unearthed (IT) AC collection network. These are concerns and hypotheses, not findings about any site.

## Evidence labels

Claims from the owner's research [1] carry its class unchanged: **confirmed**, **reasonably inferred**, **disputed**, **unverified** or **unknown**. The research deals with lightning only briefly, so this document also relies on two other sources:

- ***Owner's concern***: from the owner's notes [2][3]. The research does not class these claims.
- ***DEHN states***: from DEHN white paper WPX051 [4]. It is confirmed that DEHN says this, because the paper was opened. WPX051 is manufacturer material that presents DEHN's own products, and independent verification of its claims is **unverified**.

Formulae are standard physics.

## 1. The concern in brief

The owner's notes [3] identify two distinct mechanisms:

1. **Magnetic induction**: large DC cable loops pick up transient voltage from a nearby discharge.
2. **Conducted partial lightning currents**: earth-potential differences between the array and the distant transformer drive current through bonding conductors and SPDs, particularly along long AC collection circuits.

The notes add: "An IT earthing arrangement does not prevent either mechanism. Nor does it automatically make the induced voltage greater than on an equivalent earthed network." (*Owner's concern*.) Neither mechanism needs a ground strike to the asset itself. That is why the owner treats a nearby observed flash as a reason to inspect, and not as the cause of any fault.

## 2. Mechanism 1: magnetic induction in string loops

When the field from a stroke changes through the area enclosed by a circuit, it induces a voltage (Faraday's law):

```
V = −dΦ/dt,   where Φ = ∫ B · dA over the loop
```

For a roughly uniform field normal to a loop of area A, this becomes V ≈ −A · dB/dt. Near a long, straight channel carrying current I, the field is B = μ₀I / (2πr) at distance r. Induced voltage therefore grows with the enclosed area, with the rate at which the stroke current changes, and with closeness to the channel.

The owner's points (*Owner's concern* [2][3]):

- **Loop area, not cable length, is the critical variable.** What matters is the area enclosed by the outgoing and return paths.
- **Routing.** When positive and negative conductors run together, their opposing currents reduce the effective loop area and the induced differential voltage. Widely separated routes can form a much larger loop.
- **Scale.** DC strings can extend hundreds of metres across metal structures. The inverter, the AC collection system and the earthing network add further conductive paths. The notes describe the array as "an inductive loop, rather than necessarily an antenna that attracts lightning".
- **Wet weather.** Wet-panel capacitance can alter transient coupling to earth, but "DOES NOT automatically mean higher induced lightning voltage" [2] (see document 04).

DEHN's position fits this picture. On string-inverter sites the inverters sit close to the modules and are bonded into an intermeshed earthing system. DEHN states that this creates "a local equipotential surface", so that partial lightning currents on the DC side "are not expected" and type-2 SPDs suffice there "because only induced surges are anticipated" (*DEHN states* [4]). On this reading, induction is the main DC-side threat. That puts the weight on loop area and routing, which no SPD can fix after installation.

## 3. Mechanism 2: earth-potential rise and partial currents towards the transformer

A strike on a module or an air-termination rod is "channelled through the PV frame into the ground" and raises the local earth potential. The transformer station is usually some distance away and remains, in DEHN's simplification, "still at '0 potential'" (*DEHN states* [4]).

DEHN's Figure 2 uses the standard expression for current spreading from a point into homogeneous soil:

```
U = î · ρE / (2π · d)
```

Here î is the peak current, ρE is the soil resistivity and d is the distance from the strike. DEHN assumes 100 kA (its figure for lightning protection system class III), 200 Ωm homogeneous soil and a strike on a module. **These figures are illustrative and use DEHN's assumptions; they are not site values:**

| Distance from strike | 1 m | 30 m | 60 m | 120 m | 300 m |
|---|---|---|---|---|---|
| Voltage to remote earth | ≈3.2 MV | ≈106 kV | ≈53 kV | ≈27 kV | ≈11 kV |

They show only that tens of kilovolts between the array and a distant earth are plausible. Layered soil and a real meshed earthing system change the distribution.

On a string-inverter design, "the majority of the power cabling is on the AC side", and that cabling "acts as an equipotential bonding conductor" between the modules' local earth and the distant transformer. Partial lightning currents "take the path of the largest voltage difference and the lowest resistance". They are injected through the inverter's AC-side SPDs and flow to the transformer station's earthing system (*DEHN states* [4], Figure 3). DEHN cites IEC TR 63227 [5] for the intermeshed earthing system and for current distribution, and IEC 61643-32 for AC-side partial currents. Neither standard's text was opened, so their content is **unverified** beyond DEHN's account. The title of IEC TR 63227:2020 was confirmed from a public catalogue listing.

The notes record DEHN's contrast between two designs. On central-inverter sites, partial currents can involve long DC routes. On string-inverter sites, they can involve the AC collection cables (*Owner's concern* [3]). The concern is that this design shift moved the conducted threat onto an 800 V AC IT network whose SPDs, insulation and monitoring may not have been chosen with that threat in mind.

## 4. Type-1 and type-2 SPDs on the AC side

| | Type 1 (class I) | Type 2 (class II) |
|---|---|---|
| Duty | Partial lightning currents (10/350 µs) | Induced and switching surges (8/20 µs) |
| DEHN's view, string-inverter sites | Needed in addition on the AC side | Sufficient on the DC side; overloaded on the AC side if used alone |

DEHN states that most string inverters have only integrated type-2 SPDs on the AC side, and that additional type-1 SPDs are "necessary" so the inverter "is not damaged or destroyed" (*DEHN states* [4]). The research's maker comparison is consistent: several inverters list "DC and AC Type II SPDs", with type 1+2 as an option on some products (as the research records it [1]).

DEHN also states the following [4]:

- The external type-1 and the integrated type-2 devices must be coordinated, or "the inverter can become damaged or even destroyed". DEHN reports a coordination test of its own product to CLC/TS 61643-12:2009.
- A spark-gap type 1 shortens the impulse that reaches the inverter, while a varistor type 1 lets current through for the whole impulse. This is a manufacturer's argument for its own technology.
- In a 3+1 ("Neptune") arrangement of four 440 V AC devices, two are always in series, which gives 880 V AC for 800 V networks.
- Anti-PID units can feed in DC "up to 50 % of the string voltage", which is up to 750 V on a 1,500 V array. DEHN reports that its arrangement did not trip with superimposed peaks of 1,450 V.

The research adds constraints that are specific to IT networks [1]:

- **Minimum Uc.** On IT, line to PE, the minimum continuous operating voltage is 1.1 U, which is 880 V at 800 V (citing Schneider's Electrical Installation Guide). A 680 V varistor "conducts at the first fault unless it is in a 3+1 scheme". Sustained voltage above 880 V carries a "thermal-runaway risk". (The voltages are derived; the IT first-fault principle is **confirmed**.)
- **Sungrow's 680 V Uc.** The research records this against the 880 V minimum of Table 534.2 as a contradiction, "scheme-dependent and unresolved" (**disputed**).
- **Anti-PID peak.** The research reconstructs it at about 1,470 V to earth, "which is where DEHN's 1,450 V test peaks sit" (**reasonably inferred**).
- **Temporary overvoltage.** DEHN gives 2,000 V for 800 V systems under IEC 60364-4-44. The research traces this to the IT rule that line voltage replaces U0, which it **confirmed** in an Italian national text. The IEC table was not opened (**unverified** verbatim). DEHN argues that on free-field PV this temporary overvoltage can "under certain conditions, be ignored" (*DEHN states*; not independently assessed).

## 5. Pylons and overhead lines

The owner is concerned that plants near transmission pylons or overhead lines may be more exposed to both mechanisms. Neither the research nor WPX051 addresses this, so it is an *owner's hypothesis* and **unverified**. The reasoning is qualitative:

- Tall earthed structures with overhead earth wires are likely strike points. A tower strike injects current through the tower's own electrodes. That may raise earth potential near part of an array or a cable route, and it puts a stroke channel close to string loops.
- An overhead line can carry surges to the transformer's HV side, which is a path separate from a strike within the array.
- The research records Huawei's instruction to use a dedicated transformer, "not LV overhead lines" [1]. That is an installation rule, not a lightning analysis.

The sources give no figures. Separation, tower earthing, soil and routing differ from site to site, so only a site study can quantify this concern.

## 6. Why IT earthing prevents neither mechanism

IT describes how the *power system* is referenced: the LV star point is not earthed. Frames, enclosures, armour and the transformer station all stay bonded to one intermeshed earthing system [4]. Therefore:

- **Induction** depends on loop area, routing and dΦ/dt. The source's earthing arrangement does not enter Faraday's law (*Owner's concern* [3]).
- **Earth-potential rise** depends on electrodes, soil and bonding. SPDs to PE provide the path towards the transformer station whether the star point is earthed or not (*DEHN states* [4]).
- **IT is not automatically worse** for induced voltage [3].

IT does add constraints of its own. SPD Uc must be chosen against line voltage to earth. Anti-PID adds a DC offset. A standing first fault that a single IMD may not have flagged (documents 01 and 02) already holds the healthy phases at up to 800 V to earth before any surge arrives (voltages derived; principle **confirmed** [1]).

Lightning also affects monitoring. The research records an EPC programme, reported by Sandia, that raised a current-sense relay's delay "from 1 to 3 s against lightning". The research describes this as "not a controlled trial" [1]. Desensitising detection to avoid lightning trips also widens the window in which real faults go unnoticed.

## 7. What the owner suggests a design should show

These are questions for a design, not requirements [1][2][3]:

1. Small DC loop area, with positive and negative conductors routed together, shown on drawings.
2. A meshed earthing and bonding network sized for the expected partial-current paths between the array and the transformer station.
3. AC-side SPDs justified for conducted partial lightning currents, using type 1 where the study needs it, with tested coordination against the integrated type-2 devices.
4. SPD Uc, scheme, temporary-overvoltage withstand and DC-offset behaviour checked for 800 V IT, supported by "the maker's written statement for 800 V IT with DC offset" [1].
5. Proximity to pylons and overhead lines recorded and assessed in the site's lightning risk study.

## 8. Open questions

- What IEC TR 63227 and IEC 61643-32 require for string-inverter AC sides (**unverified**: not opened).
- Whether a 3+1 scheme resolves the 680 V against 880 V Uc question on a given product (**disputed**).
- How much pylon or overhead-line proximity changes exposure (**unknown**).
- How often lightning damages string-inverter IT plants in the field (**unknown**). The research found that incident data are not structured to capture earth faults [1].

---

*Engineering discussion of risks and hypotheses. Not a design, compliance determination or site-specific protection study; apply to a site only through a qualified engineer's study of that site's data.*

## References

1. Owner's research, "Earthing, inverter topology and protection in utility-scale solar", 21 October 2026 (anonymised, evidence-classed).
2. Owner's notes, "Wet weather, solar panel capacitance and lightning", 22 October 2026.
3. Owner's notes, "Solar farms: lightning, cable loops and IT earthing".
4. DEHN SE, *Protection of 800 V AC String Inverters Against Lightning Damage on the AC Side*, White Paper WPX051/EN/0422, © 2022 DEHN SE. https://www.dehn-international.com/sites/default/files/media/files/string-inverters-wpx051-en.pdf
5. IEC TR 63227:2020, *Lightning and surge voltage protection for photovoltaic (PV) power supply systems* (cited by DEHN [4]; not opened).
