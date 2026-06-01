"""
sample_run.py
Reconstructs the Neurolief / Proliv Rx Dallas run from the reference handoff
and writes the rendered HTML to sample_output.html.

Run:
    python sample_run.py
"""

from datetime import date
from pathlib import Path

from handoff_renderer import Account, Confidence, HandoffRun, Tier, render_handoff

# ── BULLSEYE (Call First) ──────────────────────────────────────────────────────

BULLSEYE_ACCOUNTS = [
    Account(
        name="NorTex Psychiatry TMS & Ketamine",
        city="Allen (75013), TX",
        phone="(214) 717-6676",
        website="nortexpsychiatry.com",
        evidence_domain="nortexpsychiatry.com",
        tier=Tier.BULLSEYE,
        confidence=Confidence.HIGH,
        internal_score=91,
        why_it_matters=(
            "Multi-modal TRD practice already running TMS, Spravato, and cash-pay ketamine. "
            "Active device and consumable needs, two named clinical decision-makers."
        ),
        wedge="Throughput across existing interventional lines. Not education.",
        confirmed_signals=[
            "NeuroStar TMS named",
            "IV ketamine $400–800",
            "Spravato on-site",
            "TRD positioning",
            "Physician-owned",
            "NP prescriber present",
        ],
        verify=[
            "TMS vendor / contract timing",
            "Monthly TRD volume",
            "Ketamine vs Spravato mix",
            "Device decision-maker",
            "Capacity bottleneck",
        ],
        landmine=(
            "Likely incumbent TMS lock-in. "
            "**Don't lead with rip-and-replace.** "
            "Aim at renewal timing, second-device logic, or workflow expansion."
        ),
    ),
    Account(
        name="Dallas Center for Advanced Depression Treatments",
        city="Addison, TX",
        phone="(469) 484-4260",
        website="prestonhollowpsychiatry.com",
        evidence_domain="prestonhollowpsychiatry.com",
        tier=Tier.BULLSEYE,
        confidence=Confidence.HIGH,
        internal_score=88,
        why_it_matters=(
            "High-influence interventional psychiatry center with Deep TMS, Spravato, and ketamine, "
            "plus regional training-site credibility. Volume and influence both high."
        ),
        wedge="KOL / reference-site value. A champion target, not just a device placement.",
        confirmed_signals=[
            "BrainsWay Deep TMS on-site",
            "Spravato training site",
            "Intranasal ketamine",
            "Cash-pay line",
            "Physician-founded",
            "Multiple mid-levels",
        ],
        verify=[
            "Referral radius",
            "Weekly treatment volume",
            "Team decision process",
            "Existing vendor satisfaction",
            "Interest as early reference site",
        ],
        landmine=(
            "They see themselves as ahead of the market. "
            "**Don't pitch basic innovation.** "
            "Treat them as a peer-level clinical influencer."
        ),
    ),
    Account(
        name="Mindful Interventions of North Texas",
        city="Frisco (75035), TX",
        phone="(972) 384-5100",
        website="mindfulinterventions.com",
        evidence_domain="mindfulinterventions.com",
        tier=Tier.BULLSEYE,
        confidence=Confidence.HIGH,
        internal_score=84,
        why_it_matters=(
            "Independent outpatient practice with confirmed TMS, IV ketamine, and esketamine protocols. "
            "Strong cash-pay orientation and single physician-owner."
        ),
        wedge="Consolidation of consumable vendors. One relationship for the full interventional stack.",
        confirmed_signals=[
            "IV ketamine suite",
            "Esketamine protocol",
            "TMS on-site",
            "Physician-owner",
            "Cash-pay focus",
        ],
        verify=[
            "Current ketamine supplier",
            "Spravato REMS enrollment",
            "TMS device age / lease terms",
            "Monthly patient volume",
        ],
        landmine=(
            "Solo practice — owner has final say on every vendor decision. "
            "**Access without alienating the gatekeeper (the front desk) is critical.**"
        ),
    ),
    Account(
        name="Plano Psychiatric & TMS Center",
        city="Plano (75024), TX",
        phone="(972) 801-0260",
        website="planopsychiatric.com",
        evidence_domain="planopsychiatric.com",
        tier=Tier.BULLSEYE,
        confidence=Confidence.MEDIUM,
        internal_score=79,
        why_it_matters=(
            "Active TMS practice with ketamine infusion listed. Physician-led, independent, "
            "and explicitly marketing TRD services. Source confidence partially limited by thin site."
        ),
        wedge="Workflow efficiency across TMS and ketamine scheduling.",
        confirmed_signals=[
            "TMS offered",
            "Ketamine infusion listed",
            "TRD positioning",
            "Physician-led",
        ],
        verify=[
            "Confirm ketamine is active vs aspirational",
            "Device brand and lease status",
            "Volume — treatments per week",
            "Who handles purchasing",
        ],
        landmine=(
            "Site was thin — signals are real but evidence depth is moderate. "
            "**Validate volume before heavy effort.**"
        ),
    ),
]

# ── CONTENDER (Validate) ───────────────────────────────────────────────────────

CONTENDER_ACCOUNTS = [
    Account(
        name="Serenity Mental Health Centers — University Park",
        city="Dallas, TX",
        phone="(817) 725-9059",
        website="serenitymentalhealthcenters.com",
        evidence_domain="serenitymentalhealthcenters.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=68,
        flags=["Multi-site — buying may be central"],
        why_it_matters=(
            "Clinical fit is real: TMS, ketamine, TRD positioning, and prescriber infrastructure are all visible."
        ),
        wedge="Authority path. Fits clinically, but find the buyer before pitching.",
        confirmed_signals=[
            "TMS offered",
            "Ketamine infusion",
            "TRD positioning",
            "Multi-modal model",
            "NP prescriber present",
        ],
        verify=[
            "Site-level buying authority",
            "Corporate procurement path",
            "Regional decision-maker",
            "Vendor approval process",
            "Can University Park pilot independently",
        ],
        landmine=(
            "**Don't treat the local clinic as the buyer until authority is confirmed.** "
            "Use the site contact to route upward if decisions are centralized."
        ),
    ),
    Account(
        name="NeuroWellness Psychiatry",
        city="Irving (75038), TX",
        phone="(214) 550-2700",
        website="neurowellnesspsychiatry.com",
        evidence_domain="neurowellnesspsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=65,
        why_it_matters="TMS practice with esketamine listed. Physician-owned and independent.",
        wedge="Expand from TMS-only to full interventional stack.",
        confirmed_signals=["TMS offered", "Esketamine listed", "Physician-owned"],
        verify=["Active esketamine vs future plan", "TMS device brand", "Monthly volume", "Budget cycle"],
        landmine="**Confirm esketamine is live before leading with Neurolief.** May be aspirational.",
    ),
    Account(
        name="Mind & Brain Wellness Center",
        city="McKinney (75070), TX",
        phone="(972) 547-8900",
        website="mindbrainwellness.com",
        evidence_domain="mindbrainwellness.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=63,
        why_it_matters="Multi-modal mental health practice with TMS and IV ketamine confirmed on site.",
        wedge="Streamlined supply for existing ketamine program.",
        confirmed_signals=["TMS offered", "IV ketamine", "Multi-modal"],
        verify=["Volume", "Purchasing lead", "Vendor relationships", "Expansion plans"],
        landmine=None,
    ),
    Account(
        name="Coppell Psychiatric Associates",
        city="Coppell (75019), TX",
        phone="(972) 304-8818",
        website="coppellpsychiatry.com",
        evidence_domain="coppellpsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=61,
        why_it_matters="Established independent psychiatry group with TMS and emerging ketamine interest.",
        wedge="First mover on ketamine within this group — position before a competitor does.",
        confirmed_signals=["TMS offered", "Psychiatry group", "Independent"],
        verify=["Ketamine status", "Group purchasing structure", "Decision-maker", "TMS contract timing"],
        landmine="Group practice — may require committee approval. **Map the decision tree first.**",
    ),
    Account(
        name="Garland Behavioral Health",
        city="Garland (75040), TX",
        phone="(972) 205-3100",
        website="garlandbehavioral.com",
        evidence_domain="garlandbehavioral.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=59,
        why_it_matters="Community psychiatry practice with TMS. Low source confidence — verify before effort.",
        wedge="TMS-to-ketamine upgrade path if volume supports it.",
        confirmed_signals=["TMS offered", "Outpatient psychiatry"],
        verify=["Ketamine interest", "Volume", "Payer mix", "Owner vs employee physicians"],
        landmine=None,
    ),
    Account(
        name="Lewisville Center for Mind Health",
        city="Lewisville (75067), TX",
        phone="(972) 420-5534",
        website="lewisvillemindhealth.com",
        evidence_domain="lewisvillemindhealth.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=57,
        why_it_matters="Small independent practice with TMS and cash-pay psychiatry positioning.",
        wedge="Ketamine as a natural next step given existing cash-pay orientation.",
        confirmed_signals=["TMS offered", "Cash-pay psychiatry"],
        verify=["Ketamine awareness", "Owner identity", "Current patient load"],
        landmine=None,
    ),
    Account(
        name="Rockwall Psychiatry & Neuroscience",
        city="Rockwall (75087), TX",
        phone="(972) 722-1140",
        website="rockwallpsychiatry.com",
        evidence_domain="rockwallpsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.MEDIUM,
        internal_score=55,
        why_it_matters="Suburban independent psychiatry with TMS. Neuroscience branding signals openness to interventional.",
        wedge="Differentiation through interventional depth in an underserved suburb.",
        confirmed_signals=["TMS offered", "Neuroscience positioning", "Independent practice"],
        verify=["Spravato or ketamine status", "Decision authority", "Practice size"],
        landmine=None,
    ),
    Account(
        name="DFW Ketamine & Wellness",
        city="Southlake (76092), TX",
        phone="(817) 310-4488",
        website="dfwketamine.com",
        evidence_domain="dfwketamine.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=52,
        why_it_matters="Ketamine-focused practice — confirms modality fit. No TMS confirmed; may be ketamine-only.",
        wedge="Spravato / esketamine add-on to existing ketamine program.",
        confirmed_signals=["IV ketamine confirmed", "Cash-pay focus"],
        verify=["TMS presence", "Spravato interest", "Physician vs CRNA-led", "Volume", "Purchasing autonomy"],
        landmine="**Ketamine-only practices may resist Spravato as a competitor.** Lead with complement, not replacement.",
    ),
    Account(
        name="Metroplex Depression & TMS",
        city="Arlington (76011), TX",
        phone="(817) 460-6767",
        website="metroplexdepression.com",
        evidence_domain="metroplexdepression.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=50,
        why_it_matters="TMS-focused depression clinic. Evidence thin on site — signals inferred from branding.",
        wedge="Ketamine or Spravato as a second-line option for TMS non-responders.",
        confirmed_signals=["TMS offered", "Depression specialty"],
        verify=["Ketamine or Spravato active", "Volume", "Decision-maker", "Vendor lock-in"],
        landmine=None,
    ),
    Account(
        name="Prosper Psychiatry Group",
        city="Prosper (75078), TX",
        phone="(972) 382-0090",
        website="prosperpsychiatry.com",
        evidence_domain="prosperpsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=48,
        why_it_matters="Growing suburb psychiatry group. TMS listed but site thin — validate before engaging.",
        wedge="Early entry in a fast-growing market node.",
        confirmed_signals=["TMS listed", "Group practice"],
        verify=["TMS active vs planned", "Ketamine interest", "Group purchasing model"],
        landmine=None,
    ),
    Account(
        name="Denton Behavioral Medicine",
        city="Denton (76201), TX",
        phone="(940) 566-8300",
        website="dentonbehavioral.com",
        evidence_domain="dentonbehavioral.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=46,
        why_it_matters="University-town outpatient psychiatry. TMS listed; Spravato presence unconfirmed.",
        wedge="Spravato REMS support as a differentiator for a practice serving complex cases.",
        confirmed_signals=["TMS offered", "Outpatient psychiatry"],
        verify=["Spravato status", "Decision authority", "Academic affiliation depth"],
        landmine=None,
    ),
    Account(
        name="Collin County Psychiatry & Wellness",
        city="Wylie (75098), TX",
        phone="(972) 941-6300",
        website="collincountypsychiatry.com",
        evidence_domain="collincountypsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=44,
        why_it_matters="Independent psychiatry with TMS. Limited site content but no disqualifying signals.",
        wedge="Ketamine program development support.",
        confirmed_signals=["TMS offered", "Independent practice"],
        verify=["Ketamine interest", "Owner identity", "Volume", "Budget availability"],
        landmine=None,
    ),
    Account(
        name="Mansfield Mindcare",
        city="Mansfield (76063), TX",
        phone="(817) 453-9800",
        website="mansfieldmindcare.com",
        evidence_domain="mansfieldmindcare.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=42,
        why_it_matters="Small independent psychiatry. TMS mentioned; no other interventional signals.",
        wedge="First interventional vendor relationship in an underserved area.",
        confirmed_signals=["TMS mentioned", "Independent"],
        verify=["Practice size", "TMS activity", "Openness to ketamine", "Decision authority"],
        landmine=None,
    ),
    Account(
        name="Lake Highlands Psychiatric Services",
        city="Dallas (75238), TX",
        phone="(214) 341-7700",
        website="lakehighlandspsych.com",
        evidence_domain="lakehighlandspsych.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=40,
        why_it_matters="Established outpatient psychiatry in east Dallas. TMS flagged but not confirmed active.",
        wedge="TRD patient flow — if TMS is active, ketamine referral pathway is natural.",
        confirmed_signals=["Psychiatry practice", "TMS listed"],
        verify=["TMS active status", "Ketamine awareness", "Volume", "Purchasing authority"],
        landmine=None,
    ),
    Account(
        name="Carrollton Comprehensive Psychiatry",
        city="Carrollton (75006), TX",
        phone="(972) 466-8888",
        evidence_domain="carrolltonpsych.com",
        website="carrolltonpsych.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=38,
        why_it_matters="Multi-physician psychiatry group. No interventional signals confirmed — prospective only.",
        wedge="Group-level contract for TMS and ketamine consumables once clinical champion identified.",
        confirmed_signals=["Multi-physician group", "Outpatient psychiatry"],
        verify=["TMS presence", "Ketamine interest", "Clinical champion", "Procurement model"],
        landmine="**Large group without confirmed interventional services — qualify clinical interest before meeting.**",
    ),
    Account(
        name="Grapevine Mind & Mood Clinic",
        city="Grapevine (76051), TX",
        phone="(817) 329-5200",
        website="grapevinemindmood.com",
        evidence_domain="grapevinemindmood.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=36,
        why_it_matters="Boutique outpatient psychiatric practice. Interventional signals weak but no disqualifiers.",
        wedge="Introduce Neurolief as first interventional option for treatment-resistant patients.",
        confirmed_signals=["Outpatient psychiatry", "Independent"],
        verify=["Interventional openness", "TRD patient volume", "Decision timeline"],
        landmine=None,
    ),
    Account(
        name="Dallas Integrative Psychiatry",
        city="Dallas (75206), TX",
        phone="(214) 827-2424",
        website="dallasintegrativepsych.com",
        evidence_domain="dallasintegrativepsych.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=34,
        why_it_matters="Integrative psychiatry branding suggests openness to non-pharmacologic interventions.",
        wedge="Neurolief as a data-backed complement to integrative protocols.",
        confirmed_signals=["Integrative psychiatry positioning"],
        verify=["TMS or ketamine present", "Patient volume", "Physician owner", "Purchasing history"],
        landmine=None,
    ),
    Account(
        name="Tarrant County Behavioral Health Associates",
        city="Fort Worth (76107), TX",
        phone="(817) 335-4800",
        website="tarrantbehavioral.com",
        evidence_domain="tarrantbehavioral.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=32,
        flags=["Multi-site — buying may be central"],
        why_it_matters="Large behavioral health group serving Tarrant County. Clinical fit unconfirmed — explore to qualify.",
        wedge="System-level contract if a clinical champion can be identified within the group.",
        confirmed_signals=["Behavioral health group", "Multi-location"],
        verify=["Interventional services present", "Purchasing structure", "Clinical champion", "Pilot site feasibility"],
        landmine="**Do not engage at site level without understanding the procurement chain.** Risk of wasted cycles.",
    ),
    Account(
        name="North Dallas Psychiatry & TMS",
        city="Dallas (75240), TX",
        phone="(972) 788-6800",
        website="northdallaspsychiatry.com",
        evidence_domain="northdallaspsychiatry.com",
        tier=Tier.CONTENDER,
        confidence=Confidence.LOW,
        internal_score=30,
        why_it_matters="TMS practice — name confirms modality. Site too thin to assess depth or volume.",
        wedge="Upgrade TMS-only practice to full interventional suite.",
        confirmed_signals=["TMS offered"],
        verify=["Device brand and lease", "Ketamine or Spravato status", "Volume", "Decision authority"],
        landmine=None,
    ),
]

# ── EXCLUDED (Suppress) ────────────────────────────────────────────────────────

EXCLUDED_ACCOUNTS = [
    Account(
        name="UT Southwestern Medical Center — Psychiatry",
        city="Dallas (75390), TX",
        phone="(214) 645-8300",
        website="utsouthwestern.edu",
        evidence_domain="utsouthwestern.edu",
        tier=Tier.EXCLUDED,
        confidence=Confidence.HIGH,
        internal_score=0,
        gate_fired="Health-system / IDN affiliation",
        evidence="UT Southwestern institutional website — academic medical center",
        suppress_reason="No independent purchasing path. Academic system procurement.",
        revisit_if="Practice spins out a private interventional clinic independently",
    ),
    Account(
        name="Parkland Health Behavioral Health Services",
        city="Dallas (75235), TX",
        phone="(214) 590-8000",
        website="parklandhealth.org",
        evidence_domain="parklandhealth.org",
        tier=Tier.EXCLUDED,
        confidence=Confidence.HIGH,
        internal_score=0,
        gate_fired="Public health system",
        evidence="Parkland Health official site — county safety-net system",
        suppress_reason="County hospital system with public procurement requirements. No commercial sales path.",
        revisit_if="Never — public institution",
    ),
    Account(
        name="Texas Health Presbyterian Hospital — Psychiatry",
        city="Dallas (75231), TX",
        phone="(214) 345-6789",
        website="texashealth.org",
        evidence_domain="texashealth.org",
        tier=Tier.EXCLUDED,
        confidence=Confidence.HIGH,
        internal_score=0,
        gate_fired="Health-system / IDN affiliation",
        evidence="Texas Health Resources system website",
        suppress_reason="System-affiliated inpatient psychiatry. IDN procurement only.",
        revisit_if="Outpatient interventional satellite opens under private physician leadership",
    ),
    Account(
        name="Methodist Dallas Medical Center — Psychiatry",
        city="Dallas (75203), TX",
        phone="(214) 947-8181",
        website="methodisthealthsystem.org",
        evidence_domain="methodisthealthsystem.org",
        tier=Tier.EXCLUDED,
        confidence=Confidence.MEDIUM,
        internal_score=0,
        gate_fired="Health-system / IDN affiliation",
        evidence="Methodist Health System website — multi-campus system",
        suppress_reason="Committee/IDN procurement. No site-level autonomy confirmed.",
        revisit_if="Site-level physician confirms purchasing autonomy",
    ),
    Account(
        name="Baylor Scott & White Psychiatry — Frisco",
        city="Frisco (75034), TX",
        phone="(469) 800-2000",
        website="bswhealth.com",
        evidence_domain="bswhealth.com",
        tier=Tier.EXCLUDED,
        confidence=Confidence.MEDIUM,
        internal_score=0,
        gate_fired="Health-system / IDN affiliation",
        evidence="Baylor Scott & White Health system site",
        suppress_reason="Large regional health system. Device decisions at IDN committee level.",
        revisit_if="Independent physician partner breaks out or confirms local purchasing authority",
    ),
    Account(
        name="Telehealth Psychiatry of Texas",
        city="Dallas (75201), TX",
        phone="(800) 555-0188",
        website="telehealthpsychiatry.com",
        evidence_domain="telehealthpsychiatry.com",
        tier=Tier.EXCLUDED,
        confidence=Confidence.HIGH,
        internal_score=0,
        gate_fired="Telehealth-only practice",
        evidence="Practice website — no physical treatment suite listed",
        suppress_reason="No in-office device or infusion capability. Telehealth-only model.",
        revisit_if="Opens a physical clinic with in-person treatment services",
    ),
]

# ── Assemble run ───────────────────────────────────────────────────────────────

RUN = HandoffRun(
    product_name="Neurolief",
    client_name="Proliv Rx",
    run_date=date(2026, 6, 1),
    specialty_label="Cash-Pay Interventional Psychiatry",
    metro="Dallas",
    icp_version="neurolief-prolivrx-cashpay-v1",
    qc_reviewer="rajiv",
    accounts=BULLSEYE_ACCOUNTS + CONTENDER_ACCOUNTS + EXCLUDED_ACCOUNTS,
    pattern_insight=(
        "The strongest accounts are not basic psychiatry clinics. They are interventional practices "
        "already running multiple TRD modalities. **Every Call First account stacks TMS, Spravato, and ketamine.** "
        "Single-modality and institutionally-affiliated practices fell to Validate or Suppress. "
        "Use that shape to triage the Validate tier fast: multi-modal independents are the ones "
        "worth the early effort."
    ),
)


if __name__ == "__main__":
    html = render_handoff(RUN, client_facing=True)
    out = Path("sample_output.html")
    out.write_text(html, encoding="utf-8")
    print(f"Rendered {len(RUN.accounts)} accounts → {out}")
    print(f"  Call First: {sum(1 for a in RUN.accounts if a.tier == Tier.BULLSEYE)}")
    print(f"  Validate:   {sum(1 for a in RUN.accounts if a.tier == Tier.CONTENDER)}")
    print(f"  Suppress:   {sum(1 for a in RUN.accounts if a.tier == Tier.EXCLUDED)}")
