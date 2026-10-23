"""The organs of the body map: where they sit, how GTEx tissues and medical terms map onto them.

Positions are in the body's own coordinates, the same as Figure Studio's ``human_body`` glyph:
``v`` runs from -1 (soles) to +1 (top of the head), ``u`` across (negative = the viewer's left,
which is the body's right side). ``size`` is the organ's drawn height in the same units.

Every organ assignment made by ``organs_for_text`` keeps the exact words that matched, so the
evidence shown for an organ always says which term put it there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Organ:
    key: str
    en: str
    glyph: str                 # Figure Studio illustration
    u: float
    v: float
    size: float                # drawn height, body units (body = 2)
    always: bool = False       # drawn (grey) even without evidence, for orientation
    mirror: bool = False       # paired organ: also drawn at -u
    rotation: float = 0.0
    sex: str | None = None     # "female" / "male": only drawn with evidence
    anchor: tuple[float, float] | None = None  # where label lines point, when not the centre


ORGANS: tuple[Organ, ...] = (
    # whole-body systems first (drawn under the organs, over the body): same centre and height as the body
    Organ("bone", "Bone & joints", "skeleton", 0.0, 0.0, 2.0, always=True, anchor=(0.09, -0.27)),
    Organ("blood", "Blood, vessels & immune", "vascular_system", 0.0, 0.0, 2.0, always=True,
          anchor=(-0.24, 0.3)),
    Organ("nerve", "Peripheral nerves", "nervous_system", 0.0, 0.0, 2.0, always=True, anchor=(-0.086, -0.7)),
    Organ("brain", "Brain & CNS", "brain", 0.0, 0.885, 0.13, always=True),
    Organ("eye", "Eye", "eye", 0.036, 0.852, 0.022, mirror=True),
    Organ("thyroid", "Thyroid", "thyroid", 0.0, 0.712, 0.045, always=True),
    Organ("esophagus", "Oesophagus", "esophagus", 0.012, 0.6, 0.14),
    Organ("lung", "Lungs", "lungs", 0.0, 0.52, 0.26, always=True),
    Organ("breast", "Breast", "breast", 0.085, 0.505, 0.06, mirror=True),
    Organ("heart", "Heart", "heart", 0.03, 0.465, 0.11, always=True),
    Organ("liver", "Liver", "liver", -0.055, 0.355, 0.12, always=True),
    Organ("stomach", "Stomach", "stomach", 0.07, 0.345, 0.11, always=True),
    Organ("spleen", "Spleen", "spleen", 0.125, 0.335, 0.07, always=True),
    Organ("adrenal", "Adrenal gland", "adrenal_gland", 0.08, 0.305, 0.025, mirror=True),
    Organ("pancreas", "Pancreas", "pancreas", 0.022, 0.285, 0.05, always=True),
    Organ("kidney", "Kidney", "kidney", 0.082, 0.248, 0.08, always=True, mirror=True),
    Organ("small_intestine", "Small intestine", "small_intestine", 0.0, 0.1, 0.14, always=True),
    Organ("colon", "Colon & rectum", "colon", 0.0, 0.105, 0.21, always=True, anchor=(-0.086, 0.09)),
    Organ("bladder", "Bladder & urinary tract", "bladder", 0.0, -0.035, 0.07, always=True),
    Organ("uterus", "Uterus & cervix", "uterus", 0.0, 0.03, 0.07, sex="female"),
    Organ("ovary", "Ovary", "ovary", 0.055, 0.05, 0.03, mirror=True, sex="female"),
    Organ("prostate", "Prostate", "prostate", 0.0, -0.075, 0.035, sex="male"),
    Organ("testis", "Testis", "testis", 0.0, -0.125, 0.045, sex="male"),
    Organ("muscle", "Muscle", "muscle", -0.235, 0.43, 0.12),
    Organ("adipose", "Fat (adipose)", "adipose_tissue", 0.14, 0.03, 0.05),
    Organ("skin", "Skin", "tissue", 0.268, 0.2, 0.035),  # a patch of skin on the forearm
)
ORGANS_BY_KEY = {o.key: o for o in ORGANS}

# GTEx tissue (tissueSiteDetailId) -> organ. Cell lines (fibroblasts, EBV lymphocytes) are not organs.
GTEX_TISSUES: dict[str, str] = {
    "Adipose_Subcutaneous": "adipose", "Adipose_Visceral_Omentum": "adipose", "Adrenal_Gland": "adrenal",
    "Artery_Aorta": "blood", "Artery_Coronary": "blood", "Artery_Tibial": "blood", "Whole_Blood": "blood",
    "Spleen": "spleen", "Bladder": "bladder", "Breast_Mammary_Tissue": "breast",
    "Cervix_Ectocervix": "uterus", "Cervix_Endocervix": "uterus", "Uterus": "uterus", "Vagina": "uterus",
    "Fallopian_Tube": "ovary", "Ovary": "ovary", "Colon_Sigmoid": "colon", "Colon_Transverse": "colon",
    "Esophagus_Gastroesophageal_Junction": "esophagus", "Esophagus_Mucosa": "esophagus",
    "Esophagus_Muscularis": "esophagus", "Heart_Atrial_Appendage": "heart", "Heart_Left_Ventricle": "heart",
    "Kidney_Cortex": "kidney", "Kidney_Medulla": "kidney", "Liver": "liver", "Lung": "lung",
    "Muscle_Skeletal": "muscle", "Nerve_Tibial": "nerve", "Pancreas": "pancreas", "Pituitary": "brain",
    "Prostate": "prostate", "Skin_Not_Sun_Exposed_Suprapubic": "skin", "Skin_Sun_Exposed_Lower_leg": "skin",
    "Small_Intestine_Terminal_Ileum": "small_intestine", "Stomach": "stomach", "Testis": "testis",
    "Thyroid": "thyroid", "Minor_Salivary_Gland": "esophagus",
}


def gtex_organ(tissue: str) -> str | None:
    if tissue in GTEX_TISSUES:
        return GTEX_TISSUES[tissue]
    if tissue.startswith("Brain_"):
        return "brain"
    return None


# Medical words -> organs. Order does not matter: a term can name several organs
# ("hepatorenal syndrome" -> liver and kidney). Patterns are matched on lower-case text.
_RULES: list[tuple[tuple[str, ...], str]] = [
    (("brain",), r"\bbrain|cerebr|cerebell|encephal|glioma|glioblastoma|astrocytoma|medulloblastoma|meningi|intracranial"
                 r"|alzheimer|parkinson|dementia|epilep|seizure|convuls|\bstroke|migraine|headache|dizziness|somnolence"
                 r"|insomnia|depress(ion|ive)|anxiety|schizophren|bipolar|psychos|psychiatric|cognitive|neurodegenerat"
                 r"|multiple sclerosis|amyotrophic|huntington|delirium|confusional|neuroblastoma|central nervous"
                 r"|hydroceph|ataxia|tremor|syncope|dysgeusia|neurotox|nervous system|pituitar|suicid"),
    (("eye",), r"\beye|ocular|\bretin|macula|glaucoma|cataract|uveit|keratit|conjunctiv|optic neur|vision|visual"
               r"|blindness|ophthalm|diplopia"),
    (("thyroid",), r"thyroid|graves|hashimoto"),
    (("esophagus",), r"oesophag|esophag|dysphagia"),
    (("lung",), r"\blungs?\b|pulmon|pneumon|bronch|asthma|\bcopd\b|respirat|dyspn|cough|pleur|alveol|emphysema"
                r"|\bnsclc\b|\bsclc\b|mesothelioma|trache|airway|\bild\b|interstitial lung|thoracic neoplasm"),
    (("heart",), r"\bheart|cardi|myocard|coronary|angina|arrhythm|atrial fib|ventricular (fib|tachy|dysf)"
                 r"|tachycardia|bradycardia|qt prolong|electrocardiogram qt|palpitation|endocard|pericard|valvul"),
    (("blood",), r"an(a)?emi|neutropeni|thrombocytopeni|leukopeni|lymphopeni|pancytopeni|ha?ematolog|ha?ematopoi"
                 r"|ha?emoglobin|leuka?emi"
                 r"|lymphoma|myeloma|\bblood\b|platelet|ha?emorrhag|bleeding|thrombo|embolism|vascul|\barter|\bvein"
                 r"|venous|hypertensi|hypotensi|aneurysm|atheroscl|sepsis|bacteri?a?emi|myelodysplas|coagul|sickle"
                 r"|thalass|ha?emophil|white blood cell|neutrophil count|lymphocyte count|immune system|immunodeficien"
                 r"|cytokine release|lymph node|lymphadenopathy|bone marrow"),
    (("liver",), r"\bliver|hepat|biliar|\bbile|cholesta|cirrho|jaundice|transaminase|alanine aminotransferase"
                 r"|aspartate aminotransferase|bilirubin|gallbladder|cholangi|\bnafld\b|\bnash\b|steatohep"),
    (("stomach",), r"stomach|gastric|gastritis|peptic|dyspeps|nausea|vomit|emesis|reflux|\bgerd\b|ulcer of the stomach"
                   r"|gastroparesis"),
    (("stomach", "small_intestine", "colon"), r"gastro-?intestinal|digestive system|abdominal pain"),
    (("pancreas",), r"pancrea|diabet|islet|insulinoma|insulin secretion|insulin deficien"),
    (("spleen",), r"splen|spleen"),
    (("kidney",), r"kidney|\brenal|nephr|glomerul|creatinine|proteinuria|ha?ematuria|dialysis|ura?emi|wilms"),
    (("adrenal",), r"adrenal|addison|cushing|pha?eochromocytoma"),
    (("small_intestine",), r"small intestin|small bowel|intestinal|\bileum|\bileal|jejun|duoden|crohn|coeliac|celiac|enteritis"
                           r"|enterocolitis|malabsorption|diarrh"),
    (("colon",), r"\bcolon|colorectal|\brectal|rectum|colitis|\banal\b|\banus|sigmoid|ca?ecum|large intestin"
                 r"|constipat|diarrh|ha?emorrhoid|\bbowel"),
    (("bladder",), r"bladder|urinar|cystitis|urothelial|urethr|incontinence|dysuria|urine retention"),
    (("kidney", "bladder"), r"urinary (tract|system)"),
    (("uterus",), r"\buter|endometri|cervix|cervical (cancer|carcinoma|neoplasm|intraepithelial)|vagin|menstru"
                  r"|menorrhag|placent|miscarriage"),
    (("ovary",), r"ovar"),
    (("prostate",), r"prostat"),
    (("testis",), r"testis|testes|testicular|testicle|azoosperm|spermatogen"),
    (("breast",), r"\bbreast|mammary|mastitis|gyna?ecomastia"),
    (("bone",), r"\bbones?\b|osteo|fracture|skelet|\bspine|vertebra|arthrit|\bjoint|\bgout|bursit|tendon|rheumatoid"
                r"|arthralgia|back pain|myeloma|musculoskeletal"),
    (("muscle",), r"\bmuscle|muscular|myalgia|myopath|myositis|rhabdomyoly|sarcopen|myasthen|creatine (phospho)?kinase"
                  r"|dystroph|musculoskeletal"),
    (("nerve",), r"neuropath|neuralgi|pa?ra?esthesi|peripheral nerv|\bnerve|numbness|hypo?a?esthesi|neuritis"
                 r"|guillain|neurotox|nervous system"),
    (("skin",), r"\bskin|cutaneous|\bderma|\brash|prurit|\bitch|eczema|psoria|\bacne|urticaria|alopecia|melanoma"
                r"|erythema|blister|photosensitiv|hand-foot|keratinocyte|basal cell carcinoma|integument|wound"),
    (("adipose",), r"adipos|obes|lipodystroph|waist"),
]
_COMPILED = [(organs, re.compile(pattern)) for organs, pattern in _RULES]

# Open Targets therapeutic areas are coarse: only the ones that name a body system are used.
THERAPEUTIC_AREAS: dict[str, tuple[str, ...]] = {
    "respiratory or thoracic disease": ("lung",), "cardiovascular disease": ("heart", "blood"),
    "nervous system disease": ("brain", "nerve"), "psychiatric disorder": ("brain",),
    "gastrointestinal disease": ("stomach", "small_intestine", "colon"), "integumentary system disease": ("skin",),
    "musculoskeletal or connective tissue disease": ("bone", "muscle"), "urinary system disease": ("kidney", "bladder"),
    "hematologic disease": ("blood",), "immune system disease": ("blood",), "pancreas disease": ("pancreas",),
    "visual system disease": ("eye",), "liver disease": ("liver",), "endocrine system disease": (),
}

# Terms that name the condition being treated rather than a side effect (FAERS lists the disease
# itself, its progression or death as "reactions"); such reports are not counted as adverse events.
DISEASE_WORDS = re.compile(r"cancer|carcinoma|neoplasm|tumou?r|metasta|lymphoma|leuka?emia|myeloma|melanoma|sarcoma"
                           r"|glioma|disease progression|malignant")


@dataclass
class TermMatch:
    organ: str
    words: str  # the text that matched


def organs_for_text(text: str) -> list[TermMatch]:
    """Organs a medical term or sentence names, with the words that matched (first match per organ)."""
    low = (text or "").lower()
    found: dict[str, str] = {}
    for organs, pattern in _COMPILED:
        m = pattern.search(low)
        if not m:
            continue
        start, end = m.start(), m.end()  # widen to whole words: "cardiovascular", not "cardi"
        while start > 0 and low[start - 1].isalnum():
            start -= 1
        while end < len(low) and low[end].isalnum():
            end += 1
        words = text[start:end] if len(text) == len(low) else low[start:end]
        for organ in organs:
            found.setdefault(organ, words)
    return [TermMatch(organ=o, words=w) for o, w in found.items()]


@dataclass
class Placement:
    """Where an organ is drawn on a body ``height`` mm tall whose centre is at (cx, cy) mm."""

    organ: Organ
    x: float
    y: float
    height: float
    extra: list[tuple[float, float]] = field(default_factory=list)  # mirrored copies


def place(organ: Organ, cx: float, cy: float, height: float) -> Placement:
    k = height / 2  # mm per body unit
    x, y = cx + organ.u * k, cy + organ.v * k
    extra = [(cx - organ.u * k, y)] if organ.mirror else []
    return Placement(organ=organ, x=x, y=y, height=organ.size * k, extra=extra)


# Body outline for the web page: the same shape as Figure Studio's human_body illustration.
_BODY_HALF = [
    (-0.035, 0.775), (-0.04, 0.715), (-0.10, 0.69), (-0.185, 0.665), (-0.225, 0.62), (-0.24, 0.52),
    (-0.255, 0.38), (-0.27, 0.24), (-0.29, 0.10), (-0.305, -0.01), (-0.315, -0.08), (-0.295, -0.135),
    (-0.265, -0.11), (-0.255, -0.03), (-0.245, 0.09), (-0.225, 0.25), (-0.205, 0.40), (-0.185, 0.50),
    (-0.165, 0.42), (-0.15, 0.30), (-0.135, 0.20), (-0.15, 0.10), (-0.168, 0.02), (-0.165, -0.09),
    (-0.15, -0.25), (-0.125, -0.45), (-0.125, -0.58), (-0.105, -0.78), (-0.08, -0.92), (-0.115, -0.975),
    (-0.10, -0.995), (-0.035, -0.99), (-0.03, -0.92), (-0.04, -0.75), (-0.035, -0.50), (-0.025, -0.28),
    (-0.012, -0.12),
]
BODY_OUTLINE = _BODY_HALF + [(0.0, -0.10)] + [(-u, v) for u, v in reversed(_BODY_HALF)]
BODY_HEAD = (0.0, 0.865, 0.088, 0.112)
# width / height of each organ's illustration (Figure Studio glyph aspect), for the web page's shapes
ASPECTS = {"brain": 1.25, "eye": 1.3, "thyroid": 0.95, "esophagus": 0.3, "lung": 0.95, "breast": 1.0, "heart": 0.9,
           "liver": 1.35, "stomach": 0.8, "spleen": 0.55, "adrenal": 0.9, "pancreas": 1.3, "kidney": 0.75,
           "small_intestine": 0.9, "colon": 1.0, "bladder": 0.8, "uterus": 1.05, "ovary": 0.75, "prostate": 0.85,
           "testis": 0.8, "blood": 0.33, "bone": 0.33, "muscle": 0.45, "nerve": 0.33, "adipose": 1.0, "skin": 2.2}
SYSTEM_SIZE = 1.5  # organs this tall (in body units) are whole-body systems


def layout_json() -> dict:
    """Organ layout for the web UI (same coordinates as the Figure Studio body map)."""
    return {
        "outline": BODY_OUTLINE, "head": BODY_HEAD,
        "organs": [{"key": o.key, "en": o.en, "glyph": o.glyph, "u": o.u, "v": o.v, "size": o.size,
                    "aspect": ASPECTS.get(o.key, 1.0), "rotation": o.rotation, "always": o.always, "mirror": o.mirror,
                    "sex": o.sex, "system": o.size >= SYSTEM_SIZE,
                    "anchor": list(o.anchor) if o.anchor else None} for o in ORGANS],
    }
