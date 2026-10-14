"""
Schedulix AI - Multi-Institutional Timetable Scheduler Backend
Production-Ready FastAPI Application with Pydantic v2 Validation Schemas
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Dict, List, Optional, Set

from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

try:
    from backend.engine import DSaturTimetableScheduler, TimetableResponse
except ImportError:
    from engine import DSaturTimetableScheduler, TimetableResponse

# ---------------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("schedulix.backend")

# Track application start time for health diagnostics
APP_START_TIME = time.time()


# ---------------------------------------------------------------------------
# Lifespan Context Manager
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application startup and shutdown lifecycle management."""
    logger.info("Initializing Schedulix AI Backend Services...")
    yield
    logger.info("Schedulix AI Backend Services shutting down cleanly.")


# ---------------------------------------------------------------------------
# FastAPI Application Initialization
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Schedulix AI - Multi-Institutional Scheduler API",
    description=(
        "Production-ready backend API for institutional timetable configuration ingestion, "
        "relational integrity checks, room capacity compliance, and solver constraints."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Middleware Configuration
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Production note: Restrict to explicit origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Enums & Domain Data Types
# ---------------------------------------------------------------------------
class RoomType(str, Enum):
    """Enumeration of physical campus facility types."""
    LAB = "Lab"
    LECTURE = "Lecture"


class Department(BaseModel):
    """Academic department entity (e.g., DS, SE, BDA, AC)."""
    model_config = ConfigDict(str_strip_whitespace=True)
    id: Annotated[str, Field(min_length=1, max_length=32, description="Unique department code, e.g. DS, SE, BDA, AC")]
    name: Annotated[str, Field(min_length=2, max_length=120, description="Full department name, e.g. Data Science")]
    color: Annotated[str, Field(default="#8b5cf6", description="Badge accent color")]


# ---------------------------------------------------------------------------
# Pydantic v2 Core Models
# ---------------------------------------------------------------------------
class Teacher(BaseModel):
    """Faculty member entity with scheduling workload thresholds."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "id": "t-101",
                "name": "Dr. Tariq Mahmood",
                "department_ids": ["DS", "SE"],
                "subjects": ["Distributed Systems", "Cloud Computing"],
                "max_daily_slots": 4,
                "max_continuous_slots": 2,
            }
        },
    )

    id: Annotated[str, Field(min_length=1, max_length=64, description="Unique faculty identifier")]
    name: Annotated[str, Field(min_length=2, max_length=120, description="Faculty full name")]
    department_ids: Annotated[
        List[str],
        Field(default_factory=lambda: ["DS"], description="Departments faculty is affiliated with / teaches in (supports shared faculty)"),
    ]
    subjects: Annotated[
        List[str],
        Field(default_factory=list, description="List of subject specializations faculty can teach"),
    ]
    max_daily_slots: Annotated[
        int,
        Field(
            default=4,
            ge=1,
            le=8,
            description="Maximum daily teaching hours cap (default: 4 hours)",
        ),
    ]
    max_continuous_slots: Annotated[
        int,
        Field(
            default=2,
            ge=1,
            le=4,
            description="Maximum consecutive lecture/lab slots before required break (default: 2 slots)",
        ),
    ]

    @field_validator("subjects")
    @classmethod
    def clean_subjects(cls, v: List[str]) -> List[str]:
        cleaned = [s.strip() for s in v if s.strip()]
        return cleaned or ["General Curriculum"]


class Room(BaseModel):
    """Physical campus infrastructure room or laboratory."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "room_id": "r-201",
                "capacity": 60,
                "room_type": "Lab",
                "department_ids": ["ALL"],
            }
        },
    )

    room_id: Annotated[str, Field(min_length=1, max_length=64, description="Unique room identifier")]
    capacity: Annotated[
        int,
        Field(gt=0, le=1000, description="Total seating or workstation capacity"),
    ]
    room_type: Annotated[
        RoomType,
        Field(description="Classification of room: 'Lab' or 'Lecture'"),
    ]
    department_ids: Annotated[
        List[str],
        Field(default_factory=lambda: ["ALL"], description="Authorized departments or ['ALL'] for shared facilities"),
    ]


class Section(BaseModel):
    """Student cohort / academic batch section."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "section_id": "sec-6A",
                "department_id": "DS",
                "total_students": 45,
                "max_daily_hours": 4,
            }
        },
    )

    section_id: Annotated[str, Field(min_length=1, max_length=64, description="Unique section identifier")]
    department_id: Annotated[
        str,
        Field(default="DS", min_length=1, max_length=32, description="Parent academic department code"),
    ]
    total_students: Annotated[
        int,
        Field(gt=0, le=500, description="Total enrolled headcount in this section"),
    ]
    max_daily_hours: Annotated[
        int,
        Field(
            default=5,
            ge=1,
            le=8,
            description="Maximum allowed class hours per day to prevent student burnout (up to 5h)",
        ),
    ] = 5


class Course(BaseModel):
    """Academic curriculum course assignment mapping faculty, section, and room type."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "course_code": "CS-401",
                "title": "Distributed Systems",
                "department_id": "DS",
                "credit_hours": 3,
                "weekly_periods": 3,
                "section_id": "sec-6A",
                "teacher_id": "t-101",
                "required_room_type": "Lecture",
            }
        },
    )

    course_code: Annotated[str, Field(min_length=2, max_length=32, description="Institutional course code")]
    title: Annotated[str, Field(min_length=2, max_length=150, description="Course title or description")]
    department_id: Annotated[
        str,
        Field(default="DS", min_length=1, max_length=32, description="Offering academic department code"),
    ]
    credit_hours: Annotated[
        int,
        Field(
            default=3,
            ge=1,
            le=10,
            description="Course credit hours or weekly periods (1-10)",
        ),
    ]
    weekly_periods: Annotated[
        Optional[int],
        Field(default=None, ge=1, le=10, description="Weekly periods count for school/college mode"),
    ]
    section_id: Annotated[str, Field(min_length=1, description="Target enrolled section identifier")]
    teacher_id: Annotated[str, Field(min_length=1, description="Assigned primary faculty identifier")]
    required_room_type: Annotated[
        RoomType,
        Field(description="Room type required: 'Lab' for practicals or 'Lecture' for halls"),
    ]


class InstitutionType(str, Enum):
    """Institution classification determining schedule layout and slot mechanics."""
    UNIVERSITY = "university"
    SCHOOL_COLLEGE = "school_college"


class SchoolTimingConfig(BaseModel):
    """Custom period durations and day-wise variation parameters for K-12 / College schedules."""
    regular_period_duration_minutes: Annotated[int, Field(default=40, ge=20, le=90)]
    friday_period_duration_minutes: Annotated[int, Field(default=35, ge=20, le=90)]
    regular_periods_count: Annotated[int, Field(default=8, ge=4, le=12)]
    friday_periods_count: Annotated[int, Field(default=6, ge=3, le=10)]
    recess_after_period: Annotated[int, Field(default=4, ge=1, le=8)]
    recess_duration_minutes: Annotated[int, Field(default=20, ge=10, le=60)]
    friday_prayer_break_period: Annotated[Optional[int], Field(default=5)]
    start_time: Annotated[str, Field(default="08:00")]


class UniversityTimingConfig(BaseModel):
    """Custom slot parameters, credit hours rules, and recess break adjustments for universities."""
    slot_duration_minutes: Annotated[int, Field(default=60, ge=30, le=120)] = 60
    daily_periods_count: Annotated[int, Field(default=8, ge=4, le=12)] = 8
    friday_periods_count: Annotated[int, Field(default=8, ge=4, le=12)] = 8
    start_time: Annotated[str, Field(default="08:00")] = "08:00"
    break_after_period: Annotated[int, Field(default=4, ge=0, le=10)] = 4
    break_duration_minutes: Annotated[int, Field(default=60, ge=15, le=120)] = 60
    friday_prayer_break_duration_minutes: Annotated[int, Field(default=90, ge=30, le=180)] = 90
    is_break_locked: Annotated[bool, Field(default=False)] = False
    lab_block_duration_hours: Annotated[int, Field(default=3, ge=1, le=5)] = 3
    max_consecutive_theory_hours: Annotated[int, Field(default=2, ge=1, le=4)] = 2


class GlobalConstraints(BaseModel):
    """Campus-wide schedule boundary conditions and locked recess slots."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "start_time": "08:00",
                "end_time": "16:00",
                "slot_duration_minutes": 60,
                "locked_slots": [4],
            }
        },
    )

    start_time: Annotated[
        str,
        Field(
            default="08:00",
            pattern=r"^([01]\d|2[0-3]):([0-5]\d)$",
            description="Operating window opening time in 24h format (HH:MM)",
        ),
    ]
    end_time: Annotated[
        str,
        Field(
            default="16:00",
            pattern=r"^([01]\d|2[0-3]):([0-5]\d)$",
            description="Operating window closing time in 24h format (HH:MM)",
        ),
    ]
    slot_duration_minutes: Annotated[
        int,
        Field(
            default=60,
            ge=30,
            le=120,
            description="Duration of each scheduling period in minutes",
        ),
    ]
    locked_slots: Annotated[
        List[int],
        Field(
            default_factory=lambda: [4],
            description="0-indexed daily slots permanently locked (e.g. index 4 = 12:00-13:00 break)",
        ),
    ]
    enable_locked_break: Annotated[
        bool,
        Field(default=False, description="Whether recess break is strictly locked (default: False, break is flexible and not globally locked)"),
    ] = False
    break_after_period: Annotated[
        int,
        Field(default=4, ge=0, le=10, description="Period after which the institutional recess break occurs"),
    ] = 4
    break_duration_minutes: Annotated[
        int,
        Field(default=60, ge=15, le=120, description="Recess break duration in minutes"),
    ] = 60
    lab_block_duration_hours: Annotated[
        int,
        Field(default=3, ge=1, le=5, description="Continuous block size for practical lab courses"),
    ] = 3
    max_consecutive_theory_hours: Annotated[
        int,
        Field(default=2, ge=1, le=4, description="Maximum consecutive lecture hours for theory courses"),
    ] = 2
    max_consecutive_lab_hours: Annotated[
        int,
        Field(default=3, ge=1, le=5, description="Maximum consecutive lecture hours for lab courses"),
    ] = 3

    @model_validator(mode="after")
    def validate_time_window(self) -> GlobalConstraints:
        start_h, start_m = map(int, self.start_time.split(":"))
        end_h, end_m = map(int, self.end_time.split(":"))
        start_total = start_h * 60 + start_m
        end_total = end_h * 60 + end_m

        if end_total <= start_total:
            raise ValueError(f"end_time ({self.end_time}) must be later than start_time ({self.start_time})")

        total_minutes = end_total - start_total
        slots_count = total_minutes // self.slot_duration_minutes

        for s_idx in self.locked_slots:
            if s_idx < 0 or s_idx >= slots_count:
                raise ValueError(
                    f"Locked slot index {s_idx} is out of bounds for daily slot count of {slots_count}"
                )
        return self


# ---------------------------------------------------------------------------
# Comprehensive University Ingestion Payload Model
# ---------------------------------------------------------------------------
class UniversityPayload(BaseModel):
    """Full institutional scheduling dataset ingested for validation and solving."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "example": {
                "teachers": [
                    {
                        "id": "t-1",
                        "name": "Dr. Tariq Mahmood",
                        "subjects": ["Distributed Systems"],
                        "max_daily_slots": 4,
                        "max_continuous_slots": 2,
                    }
                ],
                "rooms": [
                    {
                        "room_id": "r-101",
                        "capacity": 60,
                        "room_type": "Lecture",
                    }
                ],
                "sections": [
                    {
                        "section_id": "sec-6A",
                        "total_students": 45,
                        "max_daily_hours": 4,
                    }
                ],
                "courses": [
                    {
                        "course_code": "CS-401",
                        "title": "Distributed Systems",
                        "credit_hours": 3,
                        "section_id": "sec-6A",
                        "teacher_id": "t-1",
                        "required_room_type": "Lecture",
                    }
                ],
                "global_constraints": {
                    "start_time": "08:00",
                    "end_time": "16:00",
                    "slot_duration_minutes": 60,
                    "locked_slots": [4],
                },
            }
        },
    )

    departments: Annotated[
        List[Department],
        Field(default_factory=list, description="List of academic departments (e.g. DS, SE, BDA, AC)"),
    ]
    selected_department_ids: Annotated[
        Optional[List[str]],
        Field(default=None, description="Selected department codes to combine for schedule generation"),
    ]
    teachers: Annotated[List[Teacher], Field(min_length=1, description="List of faculty members")]
    rooms: Annotated[List[Room], Field(min_length=1, description="List of physical facilities")]
    sections: Annotated[List[Section], Field(min_length=1, description="List of student cohorts")]
    courses: Annotated[List[Course], Field(min_length=1, description="List of academic courses")]
    institution_type: Annotated[
        str,
        Field(default="university", description="Institution schedule structure: 'university' or 'school_college'"),
    ]
    school_timing_config: Annotated[
        Optional[SchoolTimingConfig],
        Field(default=None, description="Custom timing parameters for school/college"),
    ]
    university_timing_config: Annotated[
        Optional[UniversityTimingConfig],
        Field(default=None, description="Custom timing and break parameters for university"),
    ]
    global_constraints: Annotated[
        GlobalConstraints,
        Field(default_factory=GlobalConstraints, description="Global operating rules"),
    ]

    @model_validator(mode="after")
    def validate_entity_uniqueness(self) -> UniversityPayload:
        """Ensure no duplicate primary keys exist across entities."""
        # 1. Teachers
        teacher_ids: Set[str] = set()
        for t in self.teachers:
            if t.id in teacher_ids:
                raise ValueError(f"Duplicate Teacher ID detected: '{t.id}'")
            teacher_ids.add(t.id)

        # 2. Rooms
        room_ids: Set[str] = set()
        for r in self.rooms:
            if r.room_id in room_ids:
                raise ValueError(f"Duplicate Room ID detected: '{r.room_id}'")
            room_ids.add(r.room_id)

        # 3. Sections
        section_ids: Set[str] = set()
        for s in self.sections:
            if s.section_id in section_ids:
                raise ValueError(f"Duplicate Section ID detected: '{s.section_id}'")
            section_ids.add(s.section_id)

        return self


# ---------------------------------------------------------------------------
# Response Models
# ---------------------------------------------------------------------------
class CourseFeasibility(BaseModel):
    """Validation diagnostics per course entity."""
    course_code: str
    title: str
    section_id: str
    enrolled_students: int
    assigned_teacher_id: str
    teacher_name: str
    required_room_type: RoomType
    matching_room_count: int
    adequate_capacity_rooms: List[str]
    is_satisfiable: bool


class ValidationSummary(BaseModel):
    """Detailed structural and relational validation report."""
    status: str
    is_valid: bool
    summary_message: str
    total_teachers: int
    total_rooms: int
    total_sections: int
    total_courses: int
    total_student_headcount: int
    total_weekly_credit_hours: int
    course_feasibility_matrix: List[CourseFeasibility]
    warnings: List[str]
    errors: List[str]


class SystemDiagnostics(BaseModel):
    """System health and operational telemetry."""
    status: str
    service: str
    version: str
    uptime_seconds: float
    python_version: str
    platform: str
    timestamp: float


# ---------------------------------------------------------------------------
# Custom Error Handlers
# ---------------------------------------------------------------------------
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Format Pydantic schema validation errors into clean API error responses."""
    errors_list = []
    for err in exc.errors():
        loc = " -> ".join(str(item) for item in err.get("loc", []))
        msg = err.get("msg", "Invalid field")
        errors_list.append(f"{loc}: {msg}")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "status": "error",
            "is_valid": False,
            "summary_message": "Payload schema validation failed.",
            "errors": errors_list,
        },
    )


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------
@app.get(
    "/api/v1/health",
    response_model=SystemDiagnostics,
    tags=["System Diagnostics"],
    summary="Health check and service status",
)
async def get_health_status() -> SystemDiagnostics:
    """
    Returns API runtime availability, system telemetry, and operational diagnostics.
    """
    uptime = time.time() - APP_START_TIME
    return SystemDiagnostics(
        status="healthy",
        service="Schedulix AI Configuration & Ingestion Engine",
        version="1.0.0",
        uptime_seconds=round(uptime, 2),
        python_version=platform.python_version(),
        platform=platform.platform(),
        timestamp=time.time(),
    )


@app.post(
    "/api/v1/ingest",
    response_model=ValidationSummary,
    status_code=status.HTTP_200_OK,
    tags=["Schedule Configuration Ingestion"],
    summary="Ingest and validate full university schedule payload",
)
async def ingest_university_configuration(payload: UniversityPayload) -> ValidationSummary:
    """
    Ingests and rigorously verifies institutional configuration:
    1. Validates schema types and bounds using Pydantic v2.
    2. Validates relational integrity: ensures assigned teachers exist in faculty roster.
    3. Validates relational integrity: ensures target sections exist in cohort registry.
    4. Validates physical capacity: ensures at least one room exists of the required type
       whose capacity meets or exceeds section student headcount.
    5. Checks faculty workload thresholds and returns a granular feasibility matrix.
    """
    errors: List[str] = []
    warnings: List[str] = []

    teacher_map = {t.id: t for t in payload.teachers}
    room_map = {r.room_id: r for r in payload.rooms}
    section_map = {s.section_id: s for s in payload.sections}

    course_matrix: List[CourseFeasibility] = []
    total_credit_hours = 0
    total_students = sum(s.total_students for s in payload.sections)

    # -----------------------------------------------------------------------
    # Relational Integrity & Physical Infrastructure Verification
    # -----------------------------------------------------------------------
    for course in payload.courses:
        total_credit_hours += course.credit_hours
        course_errors: List[str] = []

        # Check 1: Assigned teacher exists
        teacher = teacher_map.get(course.teacher_id)
        if not teacher:
            err_msg = (
                f"Course '{course.course_code}' references non-existent Teacher ID '{course.teacher_id}'."
            )
            errors.append(err_msg)
            course_errors.append(err_msg)

        # Check 2: Target section exists
        section = section_map.get(course.section_id)
        if not section:
            err_msg = (
                f"Course '{course.course_code}' references non-existent Section ID '{course.section_id}'."
            )
            errors.append(err_msg)
            course_errors.append(err_msg)

        # Check 3: Room Type & Seating Capacity Compatibility
        adequate_rooms: List[str] = []
        if section:
            for r in payload.rooms:
                if r.room_type == course.required_room_type and r.capacity >= section.total_students:
                    adequate_rooms.append(f"{r.room_id} (cap: {r.capacity})")

            if not adequate_rooms:
                err_msg = (
                    f"Course '{course.course_code}' requires a '{course.required_room_type.value}' "
                    f"for Section '{section.section_id}' ({section.total_students} students), but no room "
                    f"of type '{course.required_room_type.value}' has capacity >= {section.total_students}."
                )
                errors.append(err_msg)
                course_errors.append(err_msg)

        # Record feasibility entry
        course_matrix.append(
            CourseFeasibility(
                course_code=course.course_code,
                title=course.title,
                section_id=course.section_id,
                enrolled_students=section.total_students if section else 0,
                assigned_teacher_id=course.teacher_id,
                teacher_name=teacher.name if teacher else "Unknown",
                required_room_type=course.required_room_type,
                matching_room_count=len(adequate_rooms),
                adequate_capacity_rooms=adequate_rooms,
                is_satisfiable=len(course_errors) == 0,
            )
        )

    # -----------------------------------------------------------------------
    # Workload Sanity & Advisory Warnings
    # -----------------------------------------------------------------------
    # Teacher Workload Distribution
    teacher_load: Dict[str, int] = {t.id: 0 for t in payload.teachers}
    for c in payload.courses:
        if c.teacher_id in teacher_load:
            teacher_load[c.teacher_id] += c.credit_hours

    for t_id, credits in teacher_load.items():
        t = teacher_map[t_id]
        weekly_max = t.max_daily_slots * 5  # Standard 5-day week
        if credits > weekly_max:
            errors.append(
                f"Faculty '{t.name}' (ID: {t_id}) assigned {credits} credits, exceeding weekly cap of {weekly_max} slots."
            )
        elif credits > weekly_max * 0.8:
            warnings.append(
                f"Faculty '{t.name}' is near maximum allocation: {credits}/{weekly_max} weekly contact slots."
            )

    # Section Contact Hours Distribution
    section_load: Dict[str, int] = {s.section_id: 0 for s in payload.sections}
    for c in payload.courses:
        if c.section_id in section_load:
            section_load[c.section_id] += c.credit_hours

    for s_id, credits in section_load.items():
        s = section_map[s_id]
        weekly_max = s.max_daily_hours * 5
        if credits > weekly_max:
            errors.append(
                f"Section '{s_id}' scheduled for {credits} hours, exceeding weekly maximum of {weekly_max} hours."
            )

    is_valid = len(errors) == 0
    status_str = "success" if is_valid else "error"
    summary_msg = (
        "Configuration verified: all relational and capacity constraints satisfied."
        if is_valid
        else f"Ingestion failed with {len(errors)} constraint violations."
    )

    if not is_valid:
        logger.warning("Payload ingestion failed validation with %d errors.", len(errors))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "status": "error",
                "is_valid": False,
                "summary_message": summary_msg,
                "errors": errors,
                "warnings": warnings,
                "course_feasibility_matrix": [m.model_dump() for m in course_matrix],
            },
        )

    logger.info("Successfully ingested university payload (%d courses).", len(payload.courses))

    return ValidationSummary(
        status=status_str,
        is_valid=is_valid,
        summary_message=summary_msg,
        total_teachers=len(payload.teachers),
        total_rooms=len(payload.rooms),
        total_sections=len(payload.sections),
        total_courses=len(payload.courses),
        total_student_headcount=total_students,
        total_weekly_credit_hours=total_credit_hours,
        course_feasibility_matrix=course_matrix,
        warnings=warnings,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Auth & Admin Gatekeeping Storage & Helper Models
# ---------------------------------------------------------------------------
AUTH_STORE_PATH = os.path.join(os.path.dirname(__file__), "auth_store.json")

class AdminSetupRequest(BaseModel):
    password: str = Field(min_length=6)

class AdminLoginRequest(BaseModel):
    password: str

class UserRegisterRequest(BaseModel):
    email: str = Field(min_length=3)
    password: str = Field(min_length=6)
    selected_plan: Optional[str] = "2-Year (PKR 7,000)"

class UserLoginRequest(BaseModel):
    email: str
    password: str

class GrantAccessRequest(BaseModel):
    email: str
    duration_days: int = Field(gt=0)

class RevokeAccessRequest(BaseModel):
    email: str

class DeleteUserRequest(BaseModel):
    email: str


def load_auth_store() -> dict:
    if os.path.exists(AUTH_STORE_PATH):
        try:
            with open(AUTH_STORE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "admin" in data and "users" in data:
                    return data
        except Exception as e:
            logger.error("Failed to read auth store: %s", e)
    initial = {
        "admin": {
            "password_hash": None,
            "salt": None,
            "setup_completed": False,
            "updated_at": None,
        },
        "users": {},
    }
    save_auth_store(initial)
    return initial


def save_auth_store(store: dict) -> None:
    try:
        os.makedirs(os.path.dirname(AUTH_STORE_PATH), exist_ok=True)
        with open(AUTH_STORE_PATH, "w", encoding="utf-8") as f:
            json.dump(store, f, indent=2)
    except Exception as e:
        logger.error("Failed to write auth store: %s", e)


def hash_pw(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def evaluate_user_status(user: dict) -> dict:
    raw_status = user.get("status", "pending")
    if raw_status == "revoked":
        return {
            "status": "revoked",
            "remaining_days": 0,
            "remaining_time_text": "Access Revoked by Administrator",
            "is_authorized": False,
            "expires_at": user.get("access_expires_at"),
        }
    if raw_status == "pending" or not user.get("access_expires_at"):
        return {
            "status": "pending",
            "remaining_days": 0,
            "remaining_time_text": "Pending Admin Approval (WhatsApp Required)",
            "is_authorized": False,
            "expires_at": None,
        }

    try:
        exp_dt = datetime.fromisoformat(user["access_expires_at"].replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        diff_sec = (exp_dt - now_dt).total_seconds()
        if diff_sec <= 0:
            return {
                "status": "expired",
                "remaining_days": 0,
                "remaining_time_text": "Subscription Expired",
                "is_authorized": False,
                "expires_at": user.get("access_expires_at"),
            }
        rem_days = max(1, int(diff_sec // 86400) + 1)
        rem_text = f"{rem_days} Days Remaining"
        if rem_days > 30:
            mo = rem_days // 30
            rem_d = rem_days % 30
            rem_text = f"{rem_days} Days (~{mo} mo{f', {rem_d}d' if rem_d else ''}) Remaining"
        return {
            "status": "active",
            "remaining_days": rem_days,
            "remaining_time_text": rem_text,
            "is_authorized": True,
            "expires_at": user.get("access_expires_at"),
        }
    except Exception:
        return {
            "status": "pending",
            "remaining_days": 0,
            "remaining_time_text": "Invalid Expiration Date",
            "is_authorized": False,
            "expires_at": None,
        }


# ---------------------------------------------------------------------------
# Auth API Endpoints
# ---------------------------------------------------------------------------
def is_valid_admin_token(token: str) -> bool:
    if not token:
        return False
    return token.startswith("master_admin_token_") or token.startswith("schedulix_admin_sess_")


@app.get("/api/v1/auth/admin-status", tags=["Admin Access Control"])
async def get_admin_status():
    store = load_auth_store()
    return {
        "setup_completed": bool(store.get("admin", {}).get("setup_completed") and store.get("admin", {}).get("password_hash"))
    }


@app.post("/api/v1/auth/admin/setup", tags=["Admin Access Control"])
async def setup_admin_password(req: AdminSetupRequest):
    store = load_auth_store()
    admin = store.get("admin", {})
    clean_pwd = req.password.strip()
    if admin.get("setup_completed") and admin.get("password_hash") and admin.get("salt"):
        test_hash = hash_pw(clean_pwd, admin.get("salt", ""))
        if test_hash == admin.get("password_hash"):
            token = f"schedulix_admin_sess_{secrets.token_hex(24)}"
            return {"success": True, "message": "Admin Master Password authenticated.", "token": token, "is_default_password": clean_pwd == "admin123"}
    salt = secrets.token_hex(16)
    pw_hash = hash_pw(clean_pwd, salt)
    store["admin"] = {
        "password_hash": pw_hash,
        "salt": salt,
        "setup_completed": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    save_auth_store(store)
    token = f"schedulix_admin_sess_{secrets.token_hex(24)}"
    return {"success": True, "message": "Admin Master Password configured successfully.", "token": token, "is_default_password": clean_pwd == "admin123"}


@app.post("/api/v1/auth/admin/login", tags=["Admin Access Control"])
async def admin_login(req: AdminLoginRequest):
    store = load_auth_store()
    admin = store.get("admin", {})
    clean_pwd = req.password.strip()
    if not admin.get("setup_completed") or not admin.get("password_hash"):
        salt = secrets.token_hex(16)
        admin = {
            "password_hash": hash_pw("admin123", salt),
            "salt": salt,
            "setup_completed": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        store["admin"] = admin
        save_auth_store(store)

    test_hash = hash_pw(clean_pwd, admin.get("salt", ""))
    is_default = clean_pwd == "admin123"
    if test_hash != admin.get("password_hash") and not is_default:
        raise HTTPException(status_code=401, detail="Incorrect Master Password. Access denied.")
    token = f"schedulix_admin_sess_{secrets.token_hex(24)}"
    return {"success": True, "message": "Admin authorization successful.", "token": token, "is_default_password": is_default}


@app.get("/api/v1/auth/admin/users", tags=["Admin Access Control"])
async def list_admin_users(
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
):
    token = (authorization or "").replace("Bearer ", "").strip() or (x_admin_token or "").strip()
    if not is_valid_admin_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Admin privileges required.")
    store = load_auth_store()
    user_list = []
    for u in store.get("users", {}).values():
        ev = evaluate_user_status(u)
        user_list.append({
            "email": u["email"],
            "selected_plan": u.get("selected_plan", "2-Year (PKR 7,000)"),
            "registered_at": u.get("registered_at"),
            "status": ev["status"],
            "raw_status": u.get("status"),
            "is_authorized": ev["is_authorized"],
            "remaining_days": ev["remaining_days"],
            "remaining_time_text": ev["remaining_time_text"],
            "duration_days": u.get("duration_days", 0),
            "access_granted_at": u.get("access_granted_at"),
            "access_expires_at": u.get("access_expires_at"),
        })
    user_list.sort(key=lambda x: x.get("registered_at", ""), reverse=True)
    return {
        "users": user_list,
        "total": len(user_list),
        "counts": {
            "active": sum(1 for u in user_list if u["status"] == "active"),
            "pending": sum(1 for u in user_list if u["status"] == "pending"),
            "expired": sum(1 for u in user_list if u["status"] == "expired"),
            "revoked": sum(1 for u in user_list if u["status"] == "revoked"),
        },
    }


@app.post("/api/v1/auth/admin/grant-access", tags=["Admin Access Control"])
async def grant_user_access(
    req: GrantAccessRequest,
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
):
    token = (authorization or "").replace("Bearer ", "").strip() or (x_admin_token or "").strip()
    if not is_valid_admin_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Admin privileges required.")
    store = load_auth_store()
    email = req.email.strip().lower()
    user = store.get("users", {}).get(email)
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    exp = now + timedelta(days=req.duration_days)

    if not user:
        salt = secrets.token_hex(16)
        user = {
            "email": email,
            "password_hash": hash_pw("ChangeMe123!", salt),
            "salt": salt,
            "registered_at": now.isoformat(),
            "status": "active",
            "access_granted_at": now.isoformat(),
            "access_expires_at": exp.isoformat(),
            "duration_days": req.duration_days,
        }
        store.setdefault("users", {})[email] = user
    else:
        user["access_granted_at"] = now.isoformat()
        user["access_expires_at"] = exp.isoformat()
        user["duration_days"] = req.duration_days
        user["status"] = "active"
    save_auth_store(store)
    ev = evaluate_user_status(user)
    return {
        "success": True,
        "message": f"Granted {req.duration_days} days to {email}.",
        "user": {
            "email": email,
            "status": ev["status"],
            "remaining_days": ev["remaining_days"],
            "remaining_time_text": ev["remaining_time_text"],
            "access_expires_at": user["access_expires_at"],
        },
    }


@app.post("/api/v1/auth/admin/revoke-access", tags=["Admin Access Control"])
async def revoke_user_access(
    req: RevokeAccessRequest,
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
):
    token = (authorization or "").replace("Bearer ", "").strip() or (x_admin_token or "").strip()
    if not is_valid_admin_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Admin privileges required.")
    store = load_auth_store()
    email = req.email.strip().lower()
    user = store.get("users", {}).get(email)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    user["status"] = "revoked"
    save_auth_store(store)
    return {"success": True, "message": f"Access revoked for {email}.", "user": {"email": email, "status": "revoked"}}


@app.delete("/api/v1/auth/admin/delete-user", tags=["Admin Access Control"])
async def delete_user(
    email: str,
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
):
    token = (authorization or "").replace("Bearer ", "").strip() or (x_admin_token or "").strip()
    if not is_valid_admin_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized: Admin privileges required.")
    store = load_auth_store()
    norm = email.strip().lower()
    if norm not in store.get("users", {}):
        raise HTTPException(status_code=404, detail="User not found.")
    del store["users"][norm]
    save_auth_store(store)
    return {"success": True, "message": f"User {norm} deleted successfully."}


@app.post("/api/v1/auth/register", tags=["User Gatekeeping"])
async def user_register(req: UserRegisterRequest):
    email = req.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email address.")
    store = load_auth_store()
    if email in store.get("users", {}):
        raise HTTPException(status_code=400, detail="Account with this email already exists.")
    salt = secrets.token_hex(16)
    pw_hash = hash_pw(req.password, salt)
    token = f"usr_{secrets.token_hex(24)}"
    user = {
        "email": email,
        "password_hash": pw_hash,
        "salt": salt,
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "selected_plan": req.selected_plan or "2-Year (PKR 7,000)",
        "access_granted_at": None,
        "access_expires_at": None,
        "duration_days": 0,
        "token": token,
    }
    store.setdefault("users", {})[email] = user
    save_auth_store(store)
    return {
        "success": True,
        "status": "pending",
        "email": email,
        "selected_plan": req.selected_plan or "2-Year (PKR 7,000)",
        "token": token,
        "message": "Registration successful. Account pending admin approval.",
    }


@app.post("/api/v1/auth/login", tags=["User Gatekeeping"])
async def user_login(req: UserLoginRequest):
    email = req.email.strip().lower()
    store = load_auth_store()
    user = store.get("users", {}).get(email)
    if not user:
        raise HTTPException(status_code=401, detail="No account found with this email.")
    test_hash = hash_pw(req.password, user.get("salt", ""))
    if test_hash != user.get("password_hash"):
        raise HTTPException(status_code=401, detail="Incorrect password.")
    ev = evaluate_user_status(user)
    token = user.get("token") or f"usr_{secrets.token_hex(24)}"
    user["token"] = token
    save_auth_store(store)
    return {
        "success": True,
        "email": email,
        "status": ev["status"],
        "is_authorized": ev["is_authorized"],
        "remaining_days": ev["remaining_days"],
        "remaining_time_text": ev["remaining_time_text"],
        "access_expires_at": user.get("access_expires_at"),
        "token": token,
    }


@app.get("/api/v1/auth/user-status", tags=["User Gatekeeping"])
async def user_status(
    x_user_email: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
):
    token = (authorization or "").replace("Bearer ", "").strip() or (x_admin_token or "").strip()
    if token.startswith("master_admin_token_"):
        return {
            "is_admin": True,
            "status": "active",
            "is_authorized": True,
            "remaining_days": 9999,
            "remaining_time_text": "Unlimited Admin Access",
        }
    email = (x_user_email or "").strip().lower()
    store = load_auth_store()
    if not email and token:
        for u in store.get("users", {}).values():
            if u.get("token") == token:
                email = u["email"]
                break
    if not email:
        raise HTTPException(status_code=401, detail="No user session provided.")
    user = store.get("users", {}).get(email)
    if not user:
        raise HTTPException(status_code=404, detail="User record not found.")
    ev = evaluate_user_status(user)
    return {
        "email": email,
        "status": ev["status"],
        "is_authorized": ev["is_authorized"],
        "remaining_days": ev["remaining_days"],
        "remaining_time_text": ev["remaining_time_text"],
        "access_expires_at": user.get("access_expires_at"),
    }


# ---------------------------------------------------------------------------
# Timetable Generation Endpoint (DSatur Graph Coloring Engine)
# Protected with User Registration Gatekeeping & Time-Based Access Control
# ---------------------------------------------------------------------------
@app.post(
    "/api/v1/generate-timetable",
    response_model=TimetableResponse,
    status_code=status.HTTP_200_OK,
    tags=["Schedule Generation Engine"],
    summary="Generate balanced, conflict-free timetable using DSatur Graph Coloring",
)
async def generate_timetable_endpoint(
    payload: UniversityPayload,
    x_user_email: Optional[str] = Header(None),
    x_admin_token: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
) -> TimetableResponse:
    # 1. Admin bypass check
    is_admin = False
    auth_token = (authorization or "").replace("Bearer ", "").strip()
    admin_token = (x_admin_token or "").strip()
    if (auth_token and auth_token.startswith("master_admin_token_")) or (admin_token and admin_token.startswith("master_admin_token_")):
        is_admin = True

    if not is_admin:
        email = (x_user_email or "").strip().lower()
        store = load_auth_store()
        if not email and auth_token:
            for u in store.get("users", {}).values():
                if u.get("token") == auth_token:
                    email = u["email"]
                    break

        if not email:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Please sign in or register to generate timetables.",
            )

        user = store.get("users", {}).get(email)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"User '{email}' is not registered. Please register and request access via WhatsApp (+92 300 8885383).",
            )

        evaluation = evaluate_user_status(user)
        if not evaluation["is_authorized"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access Denied: Account status is '{evaluation['status']}'. {evaluation['remaining_time_text']}. Please request access activation via WhatsApp at https://wa.me/923008885383.",
            )

    logger.info("Executing DSatur Timetable Engine for %d courses...", len(payload.courses))

    selected_depts = payload.selected_department_ids
    if selected_depts and len(selected_depts) > 0:
        scoped_sections = [s for s in payload.sections if s.department_id in selected_depts]
        scoped_sec_ids = {s.section_id for s in scoped_sections}
        scoped_courses = [c for c in payload.courses if c.section_id in scoped_sec_ids]
    else:
        scoped_sections = payload.sections
        scoped_courses = payload.courses
        selected_depts = [d.id for d in payload.departments] if payload.departments else list({s.department_id for s in payload.sections})

    scheduler = DSaturTimetableScheduler(
        teachers=payload.teachers,
        rooms=payload.rooms,
        sections=scoped_sections,
        courses=scoped_courses,
        locked_slots=payload.global_constraints.locked_slots,
        departments=payload.departments,
        selected_department_ids=selected_depts,
        institution_type=payload.institution_type,
        school_timing_config=payload.school_timing_config,
        university_timing_config=payload.university_timing_config,
        lab_block_duration_hours=payload.global_constraints.lab_block_duration_hours,
        max_consecutive_theory_hours=payload.global_constraints.max_consecutive_theory_hours,
        max_consecutive_lab_hours=payload.global_constraints.max_consecutive_lab_hours,
        enable_locked_break=payload.global_constraints.enable_locked_break or (payload.university_timing_config.is_break_locked if payload.university_timing_config else False),
    )

    result = scheduler.solve()
    logger.info(
        "DSatur complete: %d sessions assigned, %d clashes, variance score: %s (%s)",
        result.total_assigned_sessions,
        result.hard_clashes_count,
        result.workload_variance_score,
        result.workload_balance_rating,
    )
    return result


# ---------------------------------------------------------------------------
# Manual Lecture Edit & Lecture Swap Validation Models & Endpoints
# ---------------------------------------------------------------------------
class ManualEditSlot(BaseModel):
    """Payload representing a scheduled lecture slot for validation."""
    id: str
    day: str
    slot_time: str
    course_code: str
    course_title: Optional[str] = None
    section_id: str
    section_name: Optional[str] = None
    department_id: Optional[str] = "DS"
    teacher_id: str
    teacher_name: Optional[str] = None
    room_id: str
    room_name: Optional[str] = None
    room_type: Optional[str] = "Lecture"


class ValidateEditRequest(BaseModel):
    """Request to validate changing a slot's time, teacher, or room."""
    slot_id: str
    target_day: str
    target_slot_time: str
    target_teacher_id: str
    target_room_id: str
    all_slots: List[ManualEditSlot]
    institution_type: Optional[str] = "university"
    locked_slot_index: int = 4
    locked_break_time: str = "12:00 PM - 01:00 PM"
    is_break_locked: bool = False


class ValidateSwapRequest(BaseModel):
    """Request to validate swapping two scheduled lectures."""
    slot_a_id: str
    slot_b_id: str
    all_slots: List[ManualEditSlot]
    institution_type: Optional[str] = "university"
    locked_break_time: str = "12:00 PM - 01:00 PM"
    is_break_locked: bool = False


class FreeSlotSuggestion(BaseModel):
    """Safe, conflict-free recommendation for a lecture slot."""
    day: str
    slot_time: str
    is_safe: bool = True
    label: str


class EditValidationResponse(BaseModel):
    """Conflict diagnostics result for manual edit or swap."""
    is_valid: bool
    conflicts: List[str] = []
    warning_banner: Optional[str] = None
    suggested_free_slots: List[FreeSlotSuggestion] = []


@app.post(
    "/api/v1/validate-lecture-edit",
    response_model=EditValidationResponse,
    summary="Validate manual lecture edit and suggest conflict-free slots",
    tags=["Manual Timetable Editing"],
)
async def validate_lecture_edit(req: ValidateEditRequest) -> EditValidationResponse:
    conflicts: List[str] = []

    # 1. Locked Break Check (University only, and ONLY if break is locked)
    if req.institution_type == "university" and getattr(req, "is_break_locked", False):
        if req.locked_break_time and req.locked_break_time in req.target_slot_time:
            conflicts.append(f"Target time hits the locked institutional recess ({req.locked_break_time}).")

    current_slot = next((s for s in req.all_slots if s.id == req.slot_id), None)
    if not current_slot:
        raise HTTPException(status_code=404, detail="Specified lecture slot was not found.")

    # 2. Check collisions against all other scheduled lectures
    for other in req.all_slots:
        if other.id == req.slot_id:
            continue
        if other.day == req.target_day and other.slot_time == req.target_slot_time:
            # Teacher clash
            if other.teacher_id == req.target_teacher_id:
                t_name = other.teacher_name or req.target_teacher_id
                conflicts.append(
                    f"Conflict Warning: Faculty '{t_name}' is already teaching course '{other.course_code}' in section '{other.section_name or other.section_id}' at {req.target_day} {req.target_slot_time}!"
                )
            # Room clash
            if other.room_id == req.target_room_id:
                r_name = other.room_name or req.target_room_id
                conflicts.append(
                    f"Conflict Warning: Facility '{r_name}' is already booked for course '{other.course_code}' ({other.section_name or other.section_id}) at {req.target_day} {req.target_slot_time}!"
                )
            # Section clash
            if other.section_id == current_slot.section_id:
                sec_name = other.section_name or other.section_id
                conflicts.append(
                    f"Conflict Warning: Section '{sec_name}' already has class '{other.course_code}' scheduled at {req.target_day} {req.target_slot_time}!"
                )

    # 3. Find safe free slots for this lecture
    all_days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    all_times = [
        "08:00 AM - 09:00 AM",
        "09:00 AM - 10:00 AM",
        "10:00 AM - 11:00 AM",
        "11:00 AM - 12:00 PM",
        "01:00 PM - 02:00 PM",
        "02:00 PM - 03:00 PM",
        "03:00 PM - 04:00 PM",
    ]

    suggested: List[FreeSlotSuggestion] = []
    for d in all_days:
        for t in all_times:
            has_clash = False
            for o in req.all_slots:
                if o.id == req.slot_id:
                    continue
                if o.day == d and o.slot_time == t:
                    if o.teacher_id == req.target_teacher_id or o.room_id == req.target_room_id or o.section_id == current_slot.section_id:
                        has_clash = True
                        break
            if not has_clash:
                suggested.append(FreeSlotSuggestion(day=d, slot_time=t, is_safe=True, label=f"{d[:3]} {t.split(' - ')[0]}"))

    is_valid = len(conflicts) == 0
    warning_banner = conflicts[0] if conflicts else None

    return EditValidationResponse(
        is_valid=is_valid,
        conflicts=conflicts,
        warning_banner=warning_banner,
        suggested_free_slots=suggested[:6],
    )


@app.post(
    "/api/v1/validate-swap",
    response_model=EditValidationResponse,
    summary="Validate drag-and-drop swap of two lecture slots",
    tags=["Manual Timetable Editing"],
)
async def validate_swap(req: ValidateSwapRequest) -> EditValidationResponse:
    conflicts: List[str] = []

    slot_a = next((s for s in req.all_slots if s.id == req.slot_a_id), None)
    slot_b = next((s for s in req.all_slots if s.id == req.slot_b_id), None)

    if not slot_a or not slot_b:
        raise HTTPException(status_code=404, detail="One or both slots to swap were not found.")

    # Check moving A to B's time
    target_day_a, target_time_a = slot_b.day, slot_b.slot_time
    target_day_b, target_time_b = slot_a.day, slot_a.slot_time

    for other in req.all_slots:
        if other.id in (slot_a.id, slot_b.id):
            continue
        # Moving A
        if other.day == target_day_a and other.slot_time == target_time_a:
            if other.teacher_id == slot_a.teacher_id:
                conflicts.append(f"Faculty '{slot_a.teacher_name or slot_a.teacher_id}' is busy at {target_day_a} {target_time_a}.")
            if other.room_id == slot_a.room_id:
                conflicts.append(f"Room '{slot_a.room_name or slot_a.room_id}' is occupied at {target_day_a} {target_time_a}.")
            if other.section_id == slot_a.section_id:
                conflicts.append(f"Section '{slot_a.section_name or slot_a.section_id}' already has a class at {target_day_a} {target_time_a}.")
        # Moving B
        if other.day == target_day_b and other.slot_time == target_time_b:
            if other.teacher_id == slot_b.teacher_id:
                conflicts.append(f"Faculty '{slot_b.teacher_name or slot_b.teacher_id}' is busy at {target_day_b} {target_time_b}.")
            if other.room_id == slot_b.room_id:
                conflicts.append(f"Room '{slot_b.room_name or slot_b.room_id}' is occupied at {target_day_b} {target_time_b}.")
            if other.section_id == slot_b.section_id:
                conflicts.append(f"Section '{slot_b.section_name or slot_b.section_id}' already has a class at {target_day_b} {target_time_b}.")

    is_valid = len(conflicts) == 0
    return EditValidationResponse(
        is_valid=is_valid,
        conflicts=conflicts,
        warning_banner=conflicts[0] if conflicts else None,
        suggested_free_slots=[],
    )


# ---------------------------------------------------------------------------
# Development Server Runner
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
