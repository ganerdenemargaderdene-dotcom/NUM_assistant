from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Text, Tuple

import re
import sqlite3
import yaml

from rasa_sdk import Action, FormValidationAction, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.events import SlotSet, EventType
from rasa_sdk.types import DomainDict

# -----------------------------
# Tuition helpers
# -----------------------------

DB_PATH = Path(__file__).resolve().parent / "tuition.db"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tuition_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            admission_group TEXT,
            faculty TEXT,
            general_credits REAL,
            major_credits REAL,
            general_rate REAL,
            major_rate REAL,
            total_tuition REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        );
        """
    )


def ensure_user(conn: sqlite3.Connection, sender_id: str) -> int:
    now = utc_now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO users(sender_id, created_at) VALUES(?, ?)",
        (sender_id, now),
    )
    row = conn.execute(
        "SELECT id FROM users WHERE sender_id = ?",
        (sender_id,),
    ).fetchone()
    return int(row[0])


def _load_pricing() -> Dict[str, Any]:
    pricing_path = Path(__file__).resolve().parent / "pricing.yml"
    with pricing_path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().replace(",", ".")
        return float(s)
    except Exception:
        return None


class ValidateTuitionForm(FormValidationAction):
    def name(self) -> Text:
        return "validate_tuition_form"

    def validate_admission_group(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        intent = tracker.latest_message.get("intent", {}).get("name")

        intent_to_group = {
            "choose_admission_before_2024_2025": "before_2024_2025",
            "choose_admission_2024_2025": "2024_2025",
            "choose_admission_2025_2026": "2025_2026",
        }

        if intent in intent_to_group:
            return {"admission_group": intent_to_group[intent]}

        allowed = {"before_2024_2025", "2024_2025", "2025_2026"}
        if slot_value in allowed:
            return {"admission_group": slot_value}

        dispatcher.utter_message(text="Сонголтоо товч дээр дарж сонгоорой.")
        return {"admission_group": None}

    def validate_faculty(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        pricing = _load_pricing()
        group = tracker.get_slot("admission_group")
        if not group or group not in pricing:
            dispatcher.utter_message(text="Эхлээд элсэлтийн оноо сонгоорой.")
            return {"faculty": None}

        faculties = set(pricing[group].keys())
        if slot_value in faculties:
            return {"faculty": slot_value}

        dispatcher.utter_message(text="Бүрэлдэхүүн/салбараа товч дээр дарж сонгоорой.")
        return {"faculty": None}

    def validate_general_credits(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        v = _to_float(slot_value)
        if v is None or v < 0:
            dispatcher.utter_message(text="Ерөнхий суурь кредитийг 0-ээс их эсвэл тэнцүү тоогоор оруулна уу.")
            return {"general_credits": None}
        return {"general_credits": v}

    def validate_major_credits(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[Text, Any]:
        v = _to_float(slot_value)
        if v is None or v < 0:
            dispatcher.utter_message(text="Мэргэжлийн суурь/мэргэших кредитийг 0-ээс их эсвэл тэнцүү тоогоор оруулна уу.")
            return {"major_credits": None}
        return {"major_credits": v}


class ActionCalculateTuition(Action):
    def name(self) -> Text:
        return "action_calculate_tuition"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[Text, Any],
    ) -> List[Dict[Text, Any]]:
        pricing = _load_pricing()

        group = tracker.get_slot("admission_group")
        faculty = tracker.get_slot("faculty")
        gen_cr = _to_float(tracker.get_slot("general_credits")) or 0.0
        maj_cr = _to_float(tracker.get_slot("major_credits")) or 0.0

        if not group or not faculty:
            dispatcher.utter_message(text="Мэдээлэл дутуу байна. Дахиад 'төлбөр бодоорой' гэж эхлүүлнэ үү.")
            return []

        try:
            rates = pricing[group][faculty]
            gen_rate = float(rates["general"])
            maj_rate = float(rates["major"])
        except Exception:
            dispatcher.utter_message(text="Уучлаарай, сонгосон өгөгдлийн үнэ хүснэгтээс олдсонгүй.")
            return []

        total = gen_cr * gen_rate + maj_cr * maj_rate

        sender_id = tracker.sender_id
        try:
            with get_conn() as conn:
                ensure_tables(conn)
                user_id = ensure_user(conn, sender_id)
                conn.execute(
                    """
                    INSERT INTO tuition_runs(
                        user_id, admission_group, faculty,
                        general_credits, major_credits,
                        general_rate, major_rate,
                        total_tuition, created_at
                    )
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        str(group),
                        str(faculty),
                        float(gen_cr),
                        float(maj_cr),
                        float(gen_rate),
                        float(maj_rate),
                        float(total),
                        utc_now_iso(),
                    ),
                )
                conn.commit()
        except Exception as e:
            dispatcher.utter_message(text=f"(DB хадгалалт амжилтгүй: {e})")

        def fmt(n: float) -> str:
            return f"{int(round(n)):,}"

        group_label = {
            "before_2024_2025": "2024–2025 оноос өмнө",
            "2024_2025": "2024–2025",
            "2025_2026": "2025–2026",
        }.get(str(group), str(group))

        msg = (
            f"Таны сонголт:\n"
            f"- Элсэлт: {group_label}\n"
            f"- Бүрэлдэхүүн/салбар: {faculty}\n\n"
            f"Тооцоолол:\n"
            f"- Ерөнхий суурь: {gen_cr} кр × {fmt(gen_rate)} ₮ = {fmt(gen_cr * gen_rate)} ₮\n"
            f"- Мэргэжлийн суурь/мэргэших: {maj_cr} кр × {fmt(maj_rate)} ₮ = {fmt(maj_cr * maj_rate)} ₮\n\n"
            f"✅ Нийт төлөх төлбөр: {fmt(total)} ₮"
        )

        dispatcher.utter_message(text=msg)
        return []


class ActionSetAdmissionBefore(Action):
    def name(self) -> Text:
        return "action_set_admission_group_before_2024_2025"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("admission_group", "before_2024_2025")]


class ActionSetAdmission2024(Action):
    def name(self) -> Text:
        return "action_set_admission_group_2024_2025"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("admission_group", "2024_2025")]


class ActionSetAdmission2025(Action):
    def name(self) -> Text:
        return "action_set_admission_group_2025_2026"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("admission_group", "2025_2026")]


class ActionSetFacultyScience(Action):
    def name(self) -> Text:
        return "action_set_faculty_science"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "ШИНЖЛЭХ УХААНЫ СУРГУУЛЬ")]


class ActionSetFacultyMTEE(Action):
    def name(self) -> Text:
        return "action_set_faculty_mtee"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "МЭДЭЭЛЛИЙН ТЕХНОЛОГИ, ЭЛЕКТРОНИКИЙН СУРГУУЛЬ")]


class ActionSetFacultyEngineering(Action):
    def name(self) -> Text:
        return "action_set_faculty_engineering"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "ИНЖЕНЕР, ТЕХНОЛОГИЙН СУРГУУЛЬ")]


class ActionSetFacultyBusiness(Action):
    def name(self) -> Text:
        return "action_set_faculty_business"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "БИЗНЕСИЙН СУРГУУЛЬ")]


class ActionSetFacultyLaw(Action):
    def name(self) -> Text:
        return "action_set_faculty_law"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "ХУУЛЬ ЗҮЙН СУРГУУЛЬ")]


class ActionSetFacultyPolitics(Action):
    def name(self) -> Text:
        return "action_set_faculty_politics"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "УЛС ТӨР СУДЛАЛ, ОЛОН УЛСЫН ХАРИЛЦАА, НИЙТИЙН УДИРДЛАГЫН СУРГУУЛЬ")]


class ActionSetFacultyZavkhan(Action):
    def name(self) -> Text:
        return "action_set_faculty_zavkhan"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "ЗАВХАН АЙМАГ ДАХЬ БИЗНЕС, МЭДЭЭЛЛИЙН ТЕХНОЛОГИЙН СУРГУУЛЬ")]


class ActionSetFacultyEast(Action):
    def name(self) -> Text:
        return "action_set_faculty_east"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "ЗҮҮН БҮСИЙН СУРГУУЛЬ")]


class ActionSetFacultyWest(Action):
    def name(self) -> Text:
        return "action_set_faculty_west"

    def run(self, dispatcher, tracker, domain):
        return [SlotSet("faculty", "БАРУУН БҮСИЙН СУРГУУЛЬ")]


# -----------------------------
# GPA helpers
# -----------------------------

@dataclass
class GradeMap:
    letter: str
    gpa: float


def score_to_grade(score: float) -> GradeMap:
    s = float(score)
    if s >= 90:
        return GradeMap("A+", 4.0)
    if 85 <= s <= 89:
        return GradeMap("A-", 3.7)
    if 80 <= s <= 84:
        return GradeMap("B+", 3.3)
    if 75 <= s <= 79:
        return GradeMap("B", 3.0)
    if 70 <= s <= 74:
        return GradeMap("C-", 1.9)
    if 65 <= s <= 69:
        return GradeMap("C", 2.0)
    if 60 <= s <= 64:
        return GradeMap("D", 1.0)
    return GradeMap("F", 0.0)


class ValidateGpaForm(FormValidationAction):
    def name(self) -> str:
        return "validate_gpa_form"

    def validate_number_of_courses(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[str, Any]:
        try:
            n = int(float(slot_value))
        except Exception:
            dispatcher.utter_message(text="Тоогоор оруулна уу. Жишээ: 2")
            return {"number_of_courses": None}

        if not (1 <= n <= 50):
            dispatcher.utter_message(text="Хичээлийн тоо 1-50 хооронд байх ёстой.")
            return {"number_of_courses": None}

        return {"number_of_courses": n, "current_course_index": 1, "courses": []}

    def validate_current_credit(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[str, Any]:
        try:
            c = float(slot_value)
        except Exception:
            dispatcher.utter_message(text="Кредитийг тоогоор оруулна уу. Жишээ: 3")
            return {"current_credit": None}

        if not (0 < c <= 30):
            dispatcher.utter_message(text="Кредит 0-30 хооронд байх ёстой.")
            return {"current_credit": None}

        return {"current_credit": c}

    def validate_current_score(
        self,
        slot_value: Any,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: DomainDict,
    ) -> Dict[str, Any]:
        try:
            s = float(slot_value)
        except Exception:
            dispatcher.utter_message(text="Дүнг тоогоор оруулна уу. Жишээ: 95")
            return {"current_score": None}

        if not (0 <= s <= 100):
            dispatcher.utter_message(text="Дүн 0-100 хооронд байх ёстой.")
            return {"current_score": None}

        n = int(tracker.get_slot("number_of_courses") or 0)
        idx = int(float(tracker.get_slot("current_course_index") or 1))
        credit = float(tracker.get_slot("current_credit") or 0)

        courses = tracker.get_slot("courses") or []
        if not isinstance(courses, list):
            courses = []

        courses.append({"credit": credit, "score": s})
        next_idx = idx + 1

        if next_idx <= n:
            return {
                "courses": courses,
                "current_course_index": next_idx,
                "current_credit": None,
                "current_score": None,
            }

        return {"courses": courses, "current_score": s}


class ActionAskCurrentCredit(Action):
    def name(self) -> str:
        return "action_ask_current_credit"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        idx = int(float(tracker.get_slot("current_course_index") or 1))
        dispatcher.utter_message(text=f"📌 {idx}-р хичээл — кредит хэд вэ? (ж: 3кр)")
        return []


class ActionAskCurrentScore(Action):
    def name(self) -> str:
        return "action_ask_current_score"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        idx = int(float(tracker.get_slot("current_course_index") or 1))
        dispatcher.utter_message(text=f"📝 {idx}-р хичээл — дүн хэд вэ? (0–100)")
        return []


class ActionCalculateGpa(Action):
    def name(self) -> str:
        return "action_calculate_gpa"

    def run(self, dispatcher: CollectingDispatcher, tracker: Tracker, domain: DomainDict) -> List[EventType]:
        courses = tracker.get_slot("courses") or []
        if not courses:
            dispatcher.utter_message(text="Хичээлийн мэдээлэл алга байна. Дахин эхлүүлье.")
            return [
                SlotSet("number_of_courses", None),
                SlotSet("current_course_index", 1),
                SlotSet("current_credit", None),
                SlotSet("current_score", None),
                SlotSet("courses", []),
            ]

        total_credits = 0.0
        total_points = 0.0
        lines: List[str] = []

        for i, c in enumerate(courses, start=1):
            cr = float(c["credit"])
            sc = float(c["score"])
            g = score_to_grade(sc)

            total_credits += cr
            total_points += cr * g.gpa

            lines.append(f"{i}. {cr:g}кр - {sc:g}% → {g.letter} ({g.gpa:.1f})")

        gpa = total_points / total_credits if total_credits > 0 else 0.0

        msg = (
            "📊 Таны дүнгийн задаргаа:\n"
            + "\n".join([f"  {ln}" for ln in lines])
            + f"\n\n✅ Нийт кредит: {total_credits:g}"
            + f"\n⭐ Нийт GPA: {gpa:.2f}"
        )

        dispatcher.utter_message(text=msg)

        return [
            SlotSet("number_of_courses", None),
            SlotSet("current_course_index", 1),
            SlotSet("current_credit", None),
            SlotSet("current_score", None),
            SlotSet("courses", []),
        ]


# -----------------------------
# Location helpers
# -----------------------------

NUM_ONLY_RE = re.compile(r"^\s*(\d{1,2})\s*$")
BAIR_RE = re.compile(r"^\s*(\d{1,2})\s*[-‐-–—]?\s*р?\s*байр\s*$", re.IGNORECASE)
BAIR_LOOSE_RE = re.compile(r"(\d{1,2})\s*[-‐-–—]?\s*р?\s*бай[аa]р", re.IGNORECASE)

FORBIDDEN = {
    ("dorm", 4),
    ("class", 6),
}


def norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("ё", "е")
    s = re.sub(r"[“”\"'`]", "", s)
    s = re.sub(r"[,\.\(\)\[\]\{\}]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def detect_kind(text: str) -> Optional[str]:
    t = norm(text)
    if "дотуур" in t or "dorm" in t:
        return "dorm"
    if "хичээл" in t or "хичээлийн" in t or "сургуулийн" in t or "academic" in t:
        return "class"
    return None


def extract_number(text: str) -> Optional[int]:
    t = text.strip()
    m = NUM_ONLY_RE.match(t)
    if m:
        return int(m.group(1))
    m = BAIR_RE.match(t)
    if m:
        return int(m.group(1))
    m = BAIR_LOOSE_RE.search(t)
    if m:
        return int(m.group(1))
    return None


def is_list_request(text: str) -> bool:
    t = norm(text)
    return t in {"байршлууд", "жагсаалт", "locations", "list", "байршилууд"}


def load_places() -> Tuple[Dict[str, Dict[str, Any]], Dict[Tuple[str, int], Dict[str, Any]], List[Dict[str, Any]]]:
    path = Path(__file__).resolve().parent / "locations.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw_places: List[Dict[str, Any]] = data.get("places", []) if isinstance(data, dict) else []

    places: List[Dict[str, Any]] = []
    for p in raw_places:
        if not isinstance(p, dict):
            continue
        kind = str(p.get("kind") or "")
        num = p.get("number")
        if isinstance(num, int) and (kind, num) in FORBIDDEN:
            continue
        places.append(p)

    alias_index: Dict[str, Dict[str, Any]] = {}
    kind_num_index: Dict[Tuple[str, int], Dict[str, Any]] = {}

    for p in places:
        aliases = p.get("aliases", []) or []
        for a in aliases:
            alias_index[norm(str(a))] = p
        kind = p.get("kind")
        num = p.get("number")
        if kind and isinstance(num, int):
            kind_num_index[(str(kind), num)] = p

    return alias_index, kind_num_index, places


_ALIAS_INDEX, _KIND_NUM_INDEX, _ALL_PLACES = load_places()


def say_place(dispatcher: CollectingDispatcher, place: Dict[str, Any]) -> None:
    title = place.get("title", "Байршил")
    url = (place.get("url") or "").strip()
    if url:
        dispatcher.utter_message(f"{title}\n{url}")
    else:
        dispatcher.utter_message(
            f"{title}\n(⚠️ Google Maps линк одоогоор locations.yml дээр байхгүй байна — линкээ нэмээд дахин туршаарай.)"
        )


class ActionSendLocation(Action):
    def name(self) -> str:
        return "action_send_location"

    def run(
        self,
        dispatcher: CollectingDispatcher,
        tracker: Tracker,
        domain: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        text = (tracker.latest_message.get("text") or "").strip()
        latest_intent = (tracker.latest_message.get("intent") or {}).get("name")

        pending_number = tracker.get_slot("pending_number")
        place_type = tracker.get_slot("place_type")

        if latest_intent == "choose_place_type" and pending_number:
            chosen_kind = detect_kind(text) or (place_type if place_type in {"class", "dorm"} else None)
            if chosen_kind is None:
                dispatcher.utter_message("“хичээлийн байр” эсвэл “дотуур байр” гэж хариулаарай 🙂")
                return []

            try:
                num = int(str(pending_number))
            except Exception:
                num = None

            if num is not None:
                if (chosen_kind, num) in FORBIDDEN:
                    dispatcher.utter_message("Уучлаарай, тэр байрны мэдээлэл энэ бот дээр байхгүй байна.")
                    return [SlotSet("pending_number", None), SlotSet("place_type", chosen_kind)]

                place = _KIND_NUM_INDEX.get((chosen_kind, num))
                if place:
                    say_place(dispatcher, place)
                    return [SlotSet("pending_number", None), SlotSet("place_type", chosen_kind)]

            dispatcher.utter_message("Уучлаарай, тэр дугаартай байршил олдсонгүй. Дахиад нэрээр нь бичээд үзээрэй.")
            return [SlotSet("pending_number", None)]

        if is_list_request(text):
            lines = ["Боломжтой байршлууд:"]
            for p in _ALL_PLACES:
                title = p.get("title")
                if title:
                    lines.append(f"• {title}")
            dispatcher.utter_message("\n".join(lines))
            return []

        kind = detect_kind(text)
        num = extract_number(text)

        if num is not None and kind is None and (NUM_ONLY_RE.match(text) or BAIR_RE.match(text) or BAIR_LOOSE_RE.search(text)):
            dispatcher.utter_message(response="utter_ask_place_type")
            return [SlotSet("pending_number", str(num))]

        if num is not None and kind in {"class", "dorm"}:
            if (kind, num) in FORBIDDEN:
                dispatcher.utter_message("Уучлаарай, тэр байрны мэдээлэл энэ бот дээр байхгүй байна.")
                return [SlotSet("place_type", kind), SlotSet("pending_number", None)]

            place = _KIND_NUM_INDEX.get((kind, num))
            if place:
                say_place(dispatcher, place)
                return [SlotSet("place_type", kind), SlotSet("pending_number", None)]

            dispatcher.utter_message("Уучлаарай, тэр дугаартай байршил олдсонгүй. Дахиад нэрээр нь бичээд үзээрэй.")
            return []

        ntext = norm(text)

        place = _ALIAS_INDEX.get(ntext)
        if place:
            say_place(dispatcher, place)
            return []

        for a_norm, p in _ALIAS_INDEX.items():
            if a_norm and a_norm in ntext:
                say_place(dispatcher, p)
                return []

        dispatcher.utter_message("Уучлаарай, тэр байршлыг олсонгүй 😅 “байршлууд” гэж бичээд жагсаалтыг хараарай.")
        return []
