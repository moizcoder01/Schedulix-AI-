"""
Schedulix AI - Timetable Scheduling Engine
Graph Coloring (DSatur) Heuristics with Workload Balancing Soft Constraints
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SLOT_LABELS = [
    "08:00 AM - 09:00 AM",
    "09:00 AM - 10:00 AM",
    "10:00 AM - 11:00 AM",
    "11:00 AM - 12:00 PM",
    "12:00 PM - 01:00 PM",  # Index 4: Locked Recess
    "01:00 PM - 02:00 PM",
    "02:00 PM - 03:00 PM",
    "03:00 PM - 04:00 PM",
    "04:00 PM - 05:00 PM",
]
LOCKED_SLOT_INDEX = 4  # 12:00 PM – 1:00 PM


# ---------------------------------------------------------------------------
# Engine Output Models
# ---------------------------------------------------------------------------
class AssignedSlot(BaseModel):
    """Individual scheduled lecture unit."""
    day: str
    day_index: int
    slot_index: int
    period: Optional[int] = None
    time_window: str
    course_code: str
    course_title: str
    section_id: str
    department_id: str = "DS"
    department_name: str = "Data Science"
    teacher_id: str
    teacher_name: str
    room_id: str
    room_name: Optional[str] = None
    room_capacity: int
    room_type: str
    is_locked_break: bool = False


class TimetableResponse(BaseModel):
    """Complete timetable output with clash diagnostics and workload variance."""
    status: str
    total_requested_sessions: int
    total_assigned_sessions: int
    hard_clashes_count: int
    unassigned_sessions_count: int
    workload_variance_score: float  # Lower is better (0.0 = perfectly uniform)
    workload_balance_rating: str    # e.g., "Optimal", "Good", "Acceptable"
    selected_department_ids: List[str] = []
    departments: List[Dict[str, Any]] = []
    assigned_slots: List[AssignedSlot]
    grid_view: Dict[str, Dict[str, Any]]
    unassigned_courses: List[Dict[str, Any]]


# ---------------------------------------------------------------------------
# Internal Graph Node Structure
# ---------------------------------------------------------------------------
@dataclass
class LectureNode:
    """Decomposed lesson block or lecture unit."""
    node_id: str
    course_code: str
    title: str
    credit_hours: int
    section_id: str
    department_id: str = "DS"
    department_name: str = "Data Science"
    section_students: int = 30
    section_max_daily: int = 5
    teacher_id: str = "t-1"
    teacher_name: str = "Faculty"
    teacher_max_daily: int = 4
    teacher_max_continuous: int = 2
    required_room_type: str = "Lecture"
    duration: int = 1
    partner_id: Optional[str] = None
    is_lab: bool = False
    neighbors: Set[str] = field(default_factory=set)
    assigned_day: Optional[int] = None
    assigned_slot: Optional[int] = None
    assigned_room: Optional[str] = None


# ---------------------------------------------------------------------------
# DSatur Engine Implementation
# ---------------------------------------------------------------------------
class DSaturTimetableScheduler:
    """
    Graph Coloring Timetable Engine based on the Degree of Saturation (DSatur)
    algorithm, augmented with Workload Balancing Soft Constraint Optimization.
    Supports both University (60m standard slots) and School/College (custom period durations & Friday split).
    """

    def __init__(
        self,
        teachers: List[Any],
        rooms: List[Any],
        sections: List[Any],
        courses: List[Any],
        locked_slots: Optional[List[int]] = None,
        departments: Optional[List[Any]] = None,
        selected_department_ids: Optional[List[str]] = None,
        institution_type: str = "university",
        school_timing_config: Optional[Any] = None,
        university_timing_config: Optional[Any] = None,
        lab_block_duration_hours: int = 3,
        max_consecutive_theory_hours: int = 2,
        max_consecutive_lab_hours: int = 3,
        enable_locked_break: bool = False,
    ):
        self.teachers_map = {t.id: t for t in teachers}
        self.rooms_list = rooms
        self.sections_map = {s.section_id: s for s in sections}
        self.courses_list = courses
        self.locked_slots = set(locked_slots or [LOCKED_SLOT_INDEX])
        self.departments_list = departments or []
        self.departments_map = {
            (d.id if hasattr(d, "id") else d["id"]): (d.name if hasattr(d, "name") else d["name"])
            for d in self.departments_list
        } if self.departments_list else {
            "DS": "Data Science",
            "SE": "Software Engineering",
            "BDA": "Big Data Analytics",
            "AC": "Applied Computing",
        }
        self.selected_department_ids = selected_department_ids or list(self.departments_map.keys())
        self.institution_type = institution_type or "university"
        self.school_timing_config = school_timing_config
        self.university_timing_config = university_timing_config
        self.lab_block_duration_hours = lab_block_duration_hours
        self.max_consecutive_theory_hours = max_consecutive_theory_hours
        self.max_consecutive_lab_hours = max_consecutive_lab_hours
        self.enable_locked_break = enable_locked_break

        # Build day-wise slot matrices & labels
        self.day_slot_labels: Dict[int, List[str]] = {}
        self.day_locked_slots: Dict[int, Set[int]] = {}
        self._build_schedule_structure()

        self.nodes: Dict[str, LectureNode] = {}
        self._build_nodes()
        self._build_conflict_graph()

    def _build_schedule_structure(self):
        """Constructs time labels and locked recess/prayer slots dynamically per day."""
        def format_m(m: int) -> str:
            h = (m // 60) % 24
            mins = m % 60
            return f"{h:02d}:{mins:02d}"

        if self.institution_type == "school_college":
            cfg = self.school_timing_config
            reg_dur = getattr(cfg, "regular_period_duration_minutes", 40) if cfg else 40
            reg_count = getattr(cfg, "regular_periods_count", 8) if cfg else 8
            recess_after = getattr(cfg, "recess_after_period", 4) if cfg else 4
            recess_dur = getattr(cfg, "recess_duration_minutes", 20) if cfg else 20
            fri_dur = getattr(cfg, "friday_period_duration_minutes", 35) if cfg else 35
            fri_count = getattr(cfg, "friday_periods_count", 6) if cfg else 6

            # Mon-Thu (Days 0, 1, 2, 3)
            mon_thu_labels: List[str] = []
            curr = 8 * 60
            for p in range(1, reg_count + 1):
                start_str = format_m(curr)
                end_str = format_m(curr + reg_dur)
                mon_thu_labels.append(f"{start_str} - {end_str}")
                curr += reg_dur
                if p == recess_after:
                    curr += recess_dur

            # Friday (Day 4: Shortened period and early dismissal / prayer)
            fri_labels: List[str] = []
            curr_f = 8 * 60
            for p in range(1, fri_count + 1):
                start_str = format_m(curr_f)
                end_str = format_m(curr_f + fri_dur)
                fri_labels.append(f"{start_str} - {end_str}")
                curr_f += fri_dur
                if p == recess_after:
                    curr_f += recess_dur

            for d in range(4):
                self.day_slot_labels[d] = mon_thu_labels
                self.day_locked_slots[d] = set()

            self.day_slot_labels[4] = fri_labels
            self.day_locked_slots[4] = set()
        else:
            # University schedule: configurable slot duration, start time, and break adjustments
            u_cfg = getattr(self, "university_timing_config", None)
            dur = getattr(u_cfg, "slot_duration_minutes", 60) if u_cfg else 60
            count = getattr(u_cfg, "daily_periods_count", 8) if u_cfg else 8
            break_after = getattr(u_cfg, "break_after_period", 4) if u_cfg else 4
            break_dur = getattr(u_cfg, "break_duration_minutes", 60) if u_cfg else 60
            start_str = getattr(u_cfg, "start_time", "08:00") if u_cfg else "08:00"
            is_locked = getattr(u_cfg, "is_break_locked", False) if u_cfg else getattr(self, "enable_locked_break", False)

            try:
                sh, sm = map(int, start_str.split(":"))
                start_m = sh * 60 + sm
            except Exception:
                start_m = 8 * 60

            fri_count = getattr(u_cfg, "friday_periods_count", count) if u_cfg else count
            fri_break_dur = getattr(u_cfg, "friday_prayer_break_duration_minutes", 90) if u_cfg else break_dur

            univ_labels: List[str] = []
            break_slot_indices: Set[int] = set()
            curr = start_m

            for p in range(1, count + 1):
                s_str = format_m(curr)
                e_str = format_m(curr + dur)
                univ_labels.append(f"{s_str} - {e_str}")
                curr += dur

                if break_after and break_after > 0 and p == break_after and break_dur > 0:
                    bs_str = format_m(curr)
                    be_str = format_m(curr + break_dur)
                    break_slot_indices.add(len(univ_labels))
                    univ_labels.append(f"{bs_str} - {be_str}")
                    curr += break_dur

            # Friday labels (accommodating prayer break duration)
            univ_fri_labels: List[str] = []
            fri_break_slot_indices: Set[int] = set()
            curr_f = start_m
            for p in range(1, fri_count + 1):
                s_str = format_m(curr_f)
                e_str = format_m(curr_f + dur)
                univ_fri_labels.append(f"{s_str} - {e_str}")
                curr_f += dur

                if break_after and break_after > 0 and p == break_after and fri_break_dur > 0:
                    bs_str = format_m(curr_f)
                    be_str = format_m(curr_f + fri_break_dur)
                    fri_break_slot_indices.add(len(univ_fri_labels))
                    univ_fri_labels.append(f"{bs_str} - {be_str}")
                    curr_f += fri_break_dur

            for d in range(4):
                self.day_slot_labels[d] = univ_labels
                # Recess break is NOT globally locked unless explicitly configured
                self.day_locked_slots[d] = set(break_slot_indices) if is_locked else set()

            self.day_slot_labels[4] = univ_fri_labels
            self.day_locked_slots[4] = set(fri_break_slot_indices) if is_locked else set()

    def _build_nodes(self):
        """Break down each Course into consecutive lesson blocks or period nodes."""
        for course in self.courses_list:
            section = self.sections_map.get(course.section_id)
            teacher = self.teachers_map.get(course.teacher_id)

            dept_id = (
                getattr(course, "department_id", None)
                or getattr(section, "department_id", None)
                or "DS"
            )
            dept_name = self.departments_map.get(dept_id, dept_id)

            students = section.total_students if section else 30
            sec_max_daily = getattr(section, "max_daily_hours", 5) if section else 5
            sec_daily_limit = min(sec_max_daily, 5)
            t_name = teacher.name if teacher else "Faculty"
            t_max_daily = teacher.max_daily_slots if teacher else 4
            t_max_cont = teacher.max_continuous_slots if teacher else 2

            is_lab = (
                (hasattr(course.required_room_type, "value") and course.required_room_type.value == "Lab")
                or str(course.required_room_type) == "Lab"
                or course.course_code.endswith("L")
                or "lab" in course.title.lower()
            )

            if self.institution_type == "school_college":
                units_count = getattr(course, "weekly_periods", None) or course.credit_hours or 3
                for unit_idx in range(units_count):
                    node_id = f"{course.course_code}_p{unit_idx}_{course.section_id}"
                    self.nodes[node_id] = LectureNode(
                        node_id=node_id,
                        course_code=course.course_code,
                        title=course.title,
                        credit_hours=units_count,
                        section_id=course.section_id,
                        department_id=dept_id,
                        department_name=dept_name,
                        section_students=students,
                        section_max_daily=sec_daily_limit,
                        teacher_id=course.teacher_id,
                        teacher_name=t_name,
                        teacher_max_daily=t_max_daily,
                        teacher_max_continuous=t_max_cont,
                        required_room_type="Lecture",
                        duration=1,
                        is_lab=False,
                    )
            elif is_lab:
                # Continuous Lab Block (Default 3h block)
                node_id = f"{course.course_code}_lab_{course.section_id}"
                self.nodes[node_id] = LectureNode(
                    node_id=node_id,
                    course_code=course.course_code,
                    title=course.title,
                    credit_hours=course.credit_hours,
                    section_id=course.section_id,
                    department_id=dept_id,
                    department_name=dept_name,
                    section_students=students,
                    section_max_daily=sec_daily_limit,
                    teacher_id=course.teacher_id,
                    teacher_name=t_name,
                    teacher_max_daily=max(t_max_daily, self.lab_block_duration_hours),
                    teacher_max_continuous=self.max_consecutive_lab_hours,
                    required_room_type="Lab",
                    duration=self.lab_block_duration_hours,
                    is_lab=True,
                )
            else:
                # Theory: 3 CH -> 2h block + 1h block on another day; 2 CH -> 2h block; 1 CH -> 1h
                if course.credit_hours == 3:
                    b1_id = f"{course.course_code}_b2h_{course.section_id}"
                    b2_id = f"{course.course_code}_b1h_{course.section_id}"
                    self.nodes[b1_id] = LectureNode(
                        node_id=b1_id,
                        course_code=course.course_code,
                        title=course.title,
                        credit_hours=3,
                        section_id=course.section_id,
                        department_id=dept_id,
                        department_name=dept_name,
                        section_students=students,
                        section_max_daily=sec_daily_limit,
                        teacher_id=course.teacher_id,
                        teacher_name=t_name,
                        teacher_max_daily=t_max_daily,
                        teacher_max_continuous=self.max_consecutive_theory_hours,
                        required_room_type="Lecture",
                        duration=2,
                        partner_id=b2_id,
                        is_lab=False,
                    )
                    self.nodes[b2_id] = LectureNode(
                        node_id=b2_id,
                        course_code=course.course_code,
                        title=course.title,
                        credit_hours=3,
                        section_id=course.section_id,
                        department_id=dept_id,
                        department_name=dept_name,
                        section_students=students,
                        section_max_daily=sec_daily_limit,
                        teacher_id=course.teacher_id,
                        teacher_name=t_name,
                        teacher_max_daily=t_max_daily,
                        teacher_max_continuous=self.max_consecutive_theory_hours,
                        required_room_type="Lecture",
                        duration=1,
                        partner_id=b1_id,
                        is_lab=False,
                    )
                elif course.credit_hours == 2:
                    b1_id = f"{course.course_code}_b2h_{course.section_id}"
                    self.nodes[b1_id] = LectureNode(
                        node_id=b1_id,
                        course_code=course.course_code,
                        title=course.title,
                        credit_hours=2,
                        section_id=course.section_id,
                        department_id=dept_id,
                        department_name=dept_name,
                        section_students=students,
                        section_max_daily=sec_daily_limit,
                        teacher_id=course.teacher_id,
                        teacher_name=t_name,
                        teacher_max_daily=t_max_daily,
                        teacher_max_continuous=self.max_consecutive_theory_hours,
                        required_room_type="Lecture",
                        duration=2,
                        is_lab=False,
                    )
                else:
                    b1_id = f"{course.course_code}_b1h_{course.section_id}"
                    self.nodes[b1_id] = LectureNode(
                        node_id=b1_id,
                        course_code=course.course_code,
                        title=course.title,
                        credit_hours=course.credit_hours or 1,
                        section_id=course.section_id,
                        department_id=dept_id,
                        department_name=dept_name,
                        section_students=students,
                        section_max_daily=sec_daily_limit,
                        teacher_id=course.teacher_id,
                        teacher_name=t_name,
                        teacher_max_daily=t_max_daily,
                        teacher_max_continuous=self.max_consecutive_theory_hours,
                        required_room_type="Lecture",
                        duration=1,
                        is_lab=False,
                    )

    def _build_conflict_graph(self):
        """
        Construct hard conflict edges (Zero Clashes):
        1. Teacher Conflict: No professor in 2 locations simultaneously.
        2. Section Conflict: No section attending 2 classes simultaneously.
        3. Course Conflict: Multiple credit units of the same course in same section.
        """
        node_ids = list(self.nodes.keys())
        n = len(node_ids)

        for i in range(n):
            u_id = node_ids[i]
            u = self.nodes[u_id]
            for j in range(i + 1, n):
                v_id = node_ids[j]
                v = self.nodes[v_id]

                # Hard conflict condition: Shared teacher, section, or same course
                if (
                    u.teacher_id == v.teacher_id
                    or u.section_id == v.section_id
                    or (u.course_code == v.course_code and u.section_id == v.section_id)
                ):
                    u.neighbors.add(v_id)
                    v.neighbors.add(u_id)

    def _get_saturation_degree(self, node: LectureNode) -> int:
        """Calculate the number of distinct forbidden timeslots from colored neighbors."""
        forbidden_timeslots: Set[Tuple[int, int]] = set()
        for neighbor_id in node.neighbors:
            neighbor = self.nodes[neighbor_id]
            if neighbor.assigned_day is not None and neighbor.assigned_slot is not None:
                forbidden_timeslots.add((neighbor.assigned_day, neighbor.assigned_slot))
        return len(forbidden_timeslots)

    def _select_next_node(self, uncolored_ids: Set[str]) -> Optional[str]:
        """
        DSatur Selection Policy:
        Choose the uncolored vertex with the maximum saturation degree.
        Tie-breaker: Highest degree in the uncolored subgraph.
        """
        if not uncolored_ids:
            return None

        best_node_id = None
        max_sat = -1
        max_uncolored_deg = -1

        for node_id in uncolored_ids:
            node = self.nodes[node_id]
            sat = self._get_saturation_degree(node)
            uncolored_deg = sum(1 for nbr in node.neighbors if nbr in uncolored_ids)

            if sat > max_sat or (sat == max_sat and uncolored_deg > max_uncolored_deg):
                max_sat = sat
                max_uncolored_deg = uncolored_deg
                best_node_id = node_id

        return best_node_id

    def solve(self) -> TimetableResponse:
        """
        Executes the DSatur graph coloring algorithm with soft constraint penalty optimization.
        """
        uncolored: Set[str] = set(self.nodes.keys())

        # Occupancy tracking:
        # room_occupancy[(day, slot, room_id)] = node_id
        room_occupancy: Dict[Tuple[int, int, str], str] = {}
        # section_schedule[(section_id, day, slot)] = node_id
        section_schedule: Dict[Tuple[str, int, int], str] = {}
        # teacher_schedule[(teacher_id, day, slot)] = node_id
        teacher_schedule: Dict[Tuple[str, int, int], str] = {}

        # Daily counts for workload balancing
        section_daily_count: Dict[Tuple[str, int], int] = {}
        teacher_daily_count: Dict[Tuple[str, int], int] = {}

        for s in self.sections_map.values():
            for d in range(5):
                section_daily_count[(s.section_id, d)] = 0

        for t in self.teachers_map.values():
            for d in range(5):
                teacher_daily_count[(t.id, d)] = 0

        unassigned_nodes: List[LectureNode] = []

        while uncolored:
            curr_id = self._select_next_node(uncolored)
            if not curr_id:
                break

            curr_node = self.nodes[curr_id]
            uncolored.remove(curr_id)

            # Find all admissible (day, start_slot, room) assignments
            admissible_candidates: List[Tuple[float, int, int, Any]] = []

            # Partner day check (Theory 2h and 1h must be on different days)
            forbidden_day: Optional[int] = None
            if curr_node.partner_id:
                partner = self.nodes.get(curr_node.partner_id)
                if partner and partner.assigned_day is not None:
                    forbidden_day = partner.assigned_day

            for day in range(5):
                if forbidden_day is not None and day == forbidden_day:
                    continue

                # Hard Constraint: Section daily hours cap (5 working hours)
                if section_daily_count.get((curr_node.section_id, day), 0) + curr_node.duration > curr_node.section_max_daily:
                    continue

                # Hard Constraint: Teacher daily slots cap
                if teacher_daily_count.get((curr_node.teacher_id, day), 0) + curr_node.duration > curr_node.teacher_max_daily:
                    continue

                slot_labels_today = self.day_slot_labels[day]
                max_start = len(slot_labels_today) - curr_node.duration

                for start_slot in range(max_start + 1):
                    span_valid = True

                    for k in range(curr_node.duration):
                        slot = start_slot + k

                        # Hard Constraint: Locked break
                        if slot in self.day_locked_slots[day]:
                            span_valid = False
                            break

                        # Hard Constraint: Section collision
                        if (curr_node.section_id, day, slot) in section_schedule:
                            span_valid = False
                            break

                        # Hard Constraint: Teacher collision
                        if (curr_node.teacher_id, day, slot) in teacher_schedule:
                            span_valid = False
                            break

                    if not span_valid:
                        continue

                    # Check available rooms with correct type & sufficient capacity
                    for room in self.rooms_list:
                        room_depts = getattr(room, "department_ids", None)
                        if room_depts and "ALL" not in room_depts and curr_node.department_id not in room_depts:
                            continue

                        r_type = (
                            room.room_type.value
                            if hasattr(room.room_type, "value")
                            else str(room.room_type)
                        )
                        if curr_node.required_room_type == "Lab" and r_type != "Lab":
                            continue
                        if curr_node.required_room_type != "Lab" and r_type == "Lab":
                            continue

                        if room.capacity < curr_node.section_students:
                            continue

                        room_free = True
                        for k in range(curr_node.duration):
                            slot = start_slot + k
                            if (day, slot, room.room_id) in room_occupancy:
                                room_free = False
                                break

                        if not room_free:
                            continue

                        # Penalty scoring
                        penalty = 0.0
                        curr_sec_day_hours = section_daily_count.get((curr_node.section_id, day), 0)
                        penalty += (curr_sec_day_hours ** 2) * 15.0

                        if curr_node.is_lab:
                            if start_slot == 5:
                                penalty -= 25.0  # Afternoon continuous lab slot
                            elif start_slot == 0:
                                penalty -= 20.0  # Morning continuous lab slot
                            else:
                                penalty += 30.0
                        else:
                            if start_slot <= 2:
                                penalty -= 15.0

                        waste = room.capacity - curr_node.section_students
                        penalty += waste * 0.1

                        admissible_candidates.append((penalty, day, start_slot, room))

            if admissible_candidates:
                admissible_candidates.sort(key=lambda x: x[0])
                _, best_day, best_slot, best_room = admissible_candidates[0]

                curr_node.assigned_day = best_day
                curr_node.assigned_slot = best_slot
                curr_node.assigned_room = best_room.room_id

                for k in range(curr_node.duration):
                    slot = best_slot + k
                    room_occupancy[(best_day, slot, best_room.room_id)] = curr_node.node_id
                    section_schedule[(curr_node.section_id, best_day, slot)] = curr_node.node_id
                    teacher_schedule[(curr_node.teacher_id, best_day, slot)] = curr_node.node_id

                section_daily_count[(curr_node.section_id, best_day)] = (
                    section_daily_count.get((curr_node.section_id, best_day), 0) + curr_node.duration
                )
                teacher_daily_count[(curr_node.teacher_id, best_day)] = (
                    teacher_daily_count.get((curr_node.teacher_id, best_day), 0) + curr_node.duration
                )
            else:
                unassigned_nodes.append(curr_node)

        # -------------------------------------------------------------------
        # Build Results & Variance Calculation
        # -------------------------------------------------------------------
        assigned_slots_list: List[AssignedSlot] = []
        grid_view: Dict[str, Dict[str, Any]] = {d: {} for d in DAYS_OF_WEEK}

        for day_idx, day_name in enumerate(DAYS_OF_WEEK):
            slots_list = self.day_slot_labels.get(day_idx, SLOT_LABELS)
            for slot_idx, slot_label in enumerate(slots_list):
                if slot_idx in self.day_locked_slots.get(day_idx, set()):
                    grid_view[day_name][slot_label] = {
                        "status": "LOCKED",
                        "title": "Recess / Prayer Recess",
                        "is_break": True,
                    }
                else:
                    grid_view[day_name][slot_label] = []

        for node in self.nodes.values():
            if node.assigned_day is not None and node.assigned_slot is not None and node.assigned_room is not None:
                day_name = DAYS_OF_WEEK[node.assigned_day]
                day_slots = self.day_slot_labels.get(node.assigned_day, SLOT_LABELS)

                room_obj = next((r for r in self.rooms_list if r.room_id == node.assigned_room), None)
                cap = room_obj.capacity if room_obj else 0
                r_type = (
                    room_obj.room_type.value
                    if room_obj and hasattr(room_obj.room_type, "value")
                    else str(room_obj.room_type if room_obj else ("Lab" if node.is_lab else "Lecture"))
                )

                for k in range(node.duration):
                    current_slot_idx = node.assigned_slot + k
                    slot_label = (
                        day_slots[current_slot_idx]
                        if current_slot_idx < len(day_slots)
                        else f"Period {current_slot_idx + 1}"
                    )

                    period_val = current_slot_idx + 1 if self.institution_type == "school_college" else (current_slot_idx if current_slot_idx >= 4 else current_slot_idx + 1)
                    slot_entry = AssignedSlot(
                        day=day_name,
                        day_index=node.assigned_day,
                        slot_index=current_slot_idx,
                        period=period_val,
                        time_window=slot_label,
                        course_code=node.course_code,
                        course_title=node.title,
                        section_id=node.section_id,
                        department_id=node.department_id,
                        department_name=node.department_name,
                        teacher_id=node.teacher_id,
                        teacher_name=node.teacher_name,
                        room_id=node.assigned_room,
                        room_name=node.assigned_room,
                        room_capacity=cap,
                        room_type=r_type,
                        is_locked_break=False,
                    )
                    assigned_slots_list.append(slot_entry)
                    if slot_label in grid_view[day_name] and isinstance(grid_view[day_name][slot_label], list):
                        grid_view[day_name][slot_label].append(slot_entry.model_dump())

        # Workload Variance Score Calculation
        daily_hours: List[int] = []
        for s in self.sections_map.values():
            for d in range(5):
                daily_hours.append(section_daily_count.get((s.section_id, d), 0))

        if daily_hours:
            mean = sum(daily_hours) / len(daily_hours)
            variance = sum((x - mean) ** 2 for x in daily_hours) / len(daily_hours)
        else:
            variance = 0.0

        variance_score = round(variance, 3)
        if variance_score < 0.6:
            balance_rating = "Optimal (Evenly Balanced)"
        elif variance_score < 1.2:
            balance_rating = "Good (Minor Distribution Delta)"
        else:
            balance_rating = "Acceptable (Heavy Workload Spikes)"

        total_requested = sum(n.duration for n in self.nodes.values())
        unassigned_count = sum(un.duration for un in unassigned_nodes)

        return TimetableResponse(
            status="success" if not unassigned_nodes else "partial",
            total_requested_sessions=total_requested,
            total_assigned_sessions=len(assigned_slots_list),
            hard_clashes_count=0,
            unassigned_sessions_count=unassigned_count,
            workload_variance_score=variance_score,
            workload_balance_rating=balance_rating,
            selected_department_ids=self.selected_department_ids,
            departments=[{"id": k, "name": v} for k, v in self.departments_map.items()],
            assigned_slots=assigned_slots_list,
            grid_view=grid_view,
            unassigned_courses=[
                {
                    "node_id": un.node_id,
                    "course_code": un.course_code,
                    "title": un.title,
                    "section_id": un.section_id,
                    "teacher_id": un.teacher_id,
                    "reason": "Capacity or faculty daily cap exhausted",
                }
                for un in unassigned_nodes
            ],
        )
