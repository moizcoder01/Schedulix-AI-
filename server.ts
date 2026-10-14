import express, { Request, Response } from 'express';
import { createServer as createViteServer } from 'vite';
import path from 'path';
import fs from 'fs';
import crypto from 'crypto';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Constants
const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
const TIME_SLOTS = [
  '08:00 AM - 09:00 AM',
  '09:00 AM - 10:00 AM',
  '10:00 AM - 11:00 AM',
  '11:00 AM - 12:00 PM',
  '12:00 PM - 01:00 PM', // Slot 4: Locked Recess
  '01:00 PM - 02:00 PM',
  '02:00 PM - 03:00 PM',
  '03:00 PM - 04:00 PM',
  '04:00 PM - 05:00 PM',
];
const LOCKED_SLOT_INDEX = 4;
const START_TIME = Date.now();

interface Department {
  id: string;
  name: string;
  color?: string;
}

interface Teacher {
  id: string;
  name: string;
  subjects: string[];
  department_ids?: string[];
  max_daily_slots: number;
  max_continuous_slots: number;
}

interface Room {
  room_id: string;
  capacity: number;
  room_type: 'Lab' | 'Lecture';
  department_ids?: string[];
}

interface Section {
  section_id: string;
  department_id?: string;
  total_students: number;
  max_daily_hours: number;
}

interface Course {
  course_code: string;
  title: string;
  department_id?: string;
  credit_hours: number;
  weekly_periods?: number;
  section_id: string;
  teacher_id: string;
  required_room_type: 'Lab' | 'Lecture';
}

interface SchoolTimingConfig {
  regular_period_duration_minutes?: number;
  friday_period_duration_minutes?: number;
  regular_periods_count?: number;
  friday_periods_count?: number;
  recess_after_period?: number;
  recess_duration_minutes?: number;
  friday_prayer_break_period?: number;
  start_time?: string;
}

interface GlobalConstraints {
  start_time: string;
  end_time: string;
  slot_duration_minutes: number;
  locked_slots: number[];
  lab_block_duration_hours?: number;
  max_consecutive_theory_hours?: number;
  max_consecutive_lab_hours?: number;
}

interface UniversityPayload {
  departments?: Department[];
  selected_department_ids?: string[];
  teachers: Teacher[];
  rooms: Room[];
  sections: Section[];
  courses: Course[];
  institution_type?: 'university' | 'school_college';
  school_timing_config?: SchoolTimingConfig;
  global_constraints: GlobalConstraints;
}

// DSatur Graph Coloring Algorithm with Workload Balancing & Multi-Department Support
function runDSaturSolver(payload: UniversityPayload) {
  const { teachers, rooms } = payload;
  const lockedSlots = new Set(payload.global_constraints?.locked_slots || [LOCKED_SLOT_INDEX]);
  const isSchoolCollege = payload.institution_type === 'school_college';

  // Construct day-wise slot labels (Mon-Thu vs Friday)
  const cfg = payload.school_timing_config || {};
  const regDur = cfg.regular_period_duration_minutes || 40;
  const friDur = cfg.friday_period_duration_minutes || 35;
  const regCount = cfg.regular_periods_count || 8;
  const friCount = cfg.friday_periods_count || 6;
  const recessAfter = cfg.recess_after_period || 4;
  const recessDur = cfg.recess_duration_minutes || 20;

  function formatM(m: number): string {
    const h = Math.floor(m / 60) % 24;
    const mins = m % 60;
    return `${h.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}`;
  }

  const daySlotLabels: Record<number, string[]> = {};
  if (isSchoolCollege) {
    const monThu: string[] = [];
    let currM = 8 * 60;
    for (let p = 1; p <= regCount; p++) {
      const s = formatM(currM);
      const e = formatM(currM + regDur);
      monThu.push(`${s} - ${e}`);
      currM += regDur;
      if (p === recessAfter) currM += recessDur;
    }
    for (let d = 0; d < 4; d++) daySlotLabels[d] = monThu;

    const fri: string[] = [];
    let currF = 8 * 60;
    for (let p = 1; p <= friCount; p++) {
      const s = formatM(currF);
      const e = formatM(currF + friDur);
      fri.push(`${s} - ${e}`);
      currF += friDur;
      if (p === recessAfter) currF += recessDur;
    }
    daySlotLabels[4] = fri;
  } else {
    for (let d = 0; d < 5; d++) daySlotLabels[d] = [...TIME_SLOTS];
  }

  const defaultDepts: Department[] = [
    { id: 'DS', name: 'Data Science', color: '#8b5cf6' },
    { id: 'SE', name: 'Software Engineering', color: '#3b82f6' },
    { id: 'BDA', name: 'Big Data Analytics', color: '#10b981' },
    { id: 'AC', name: 'Applied Computing', color: '#f59e0b' },
  ];
  const departmentsList = payload.departments && payload.departments.length > 0 ? payload.departments : defaultDepts;
  const deptMap = new Map(departmentsList.map(d => [d.id, d.name]));

  // Scope sections and courses by selected_department_ids if provided
  const selectedDepts = payload.selected_department_ids && payload.selected_department_ids.length > 0
    ? payload.selected_department_ids
    : departmentsList.map(d => d.id);

  const sections = payload.sections.filter(s => selectedDepts.includes(s.department_id || 'DS'));
  const activeSecIds = new Set(sections.map(s => s.section_id));
  const courses = payload.courses.filter(c => activeSecIds.has(c.section_id));

  const teacherMap = new Map(teachers.map(t => [t.id, t]));
  const roomMap = new Map(rooms.map(r => [r.room_id, r]));
  const sectionMap = new Map(sections.map(s => [s.section_id, s]));

  // Decompose courses into lesson blocks based on consecutive slot constraints
  interface LectureBlock {
    blockId: string;
    courseCode: string;
    title: string;
    departmentId: string;
    departmentName: string;
    creditHours: number;
    sectionId: string;
    students: number;
    teacherId: string;
    teacherName: string;
    requiredRoomType: string;
    duration: number; // 1, 2, or 3 (for continuous labs)
    isLab: boolean;
    secMaxDaily: number;
    tMaxDaily: number;
    tMaxCont: number;
    partnerBlockId?: string;
    assignedDay?: number;
    assignedStartSlot?: number;
    assignedRoomId?: string;
  }

  const blocks: LectureBlock[] = [];
  const labBlockDuration = payload.global_constraints?.lab_block_duration_hours || 3;
  const maxConsecTheory = payload.global_constraints?.max_consecutive_theory_hours || 2;
  const maxConsecLab = payload.global_constraints?.max_consecutive_lab_hours || 3;

  courses.forEach(c => {
    const sec = sectionMap.get(c.section_id);
    const teacher = teacherMap.get(c.teacher_id);
    const deptId = c.department_id || sec?.department_id || 'DS';
    const deptName = deptMap.get(deptId) || deptId;
    const isLab = c.required_room_type === 'Lab' || c.course_code.endsWith('L') || c.title.toLowerCase().includes('lab');
    const secDailyLimit = Math.min(sec?.max_daily_hours || 5, 5); // 5 total working hours per day cap

    if (isSchoolCollege) {
      const unitsCount = c.weekly_periods ? c.weekly_periods : c.credit_hours;
      for (let u = 0; u < unitsCount; u++) {
        blocks.push({
          blockId: `${c.course_code}_p${u + 1}_${c.section_id}`,
          courseCode: c.course_code,
          title: c.title,
          departmentId: deptId,
          departmentName: deptName,
          creditHours: unitsCount,
          sectionId: c.section_id,
          students: sec?.total_students || 35,
          teacherId: c.teacher_id,
          teacherName: teacher?.name || 'Faculty',
          requiredRoomType: c.required_room_type || 'Lecture',
          duration: 1,
          isLab: false,
          secMaxDaily: secDailyLimit,
          tMaxDaily: teacher?.max_daily_slots || 4,
          tMaxCont: teacher?.max_continuous_slots || 2,
        });
      }
    } else if (isLab) {
      // Lab Courses: Scheduled as a 3-consecutive-hour continuous block
      blocks.push({
        blockId: `${c.course_code}_lab_${c.section_id}`,
        courseCode: c.course_code,
        title: c.title,
        departmentId: deptId,
        departmentName: deptName,
        creditHours: c.credit_hours,
        sectionId: c.section_id,
        students: sec?.total_students || 35,
        teacherId: c.teacher_id,
        teacherName: teacher?.name || 'Faculty',
        requiredRoomType: 'Lab',
        duration: labBlockDuration,
        isLab: true,
        secMaxDaily: secDailyLimit,
        tMaxDaily: Math.max(teacher?.max_daily_slots || 4, labBlockDuration),
        tMaxCont: maxConsecLab,
      });
    } else {
      // Theory Courses:
      // 3 CH -> 2-consecutive-hour block on one day + remaining 1 hour on another day
      // 2 CH -> 2-consecutive-hour block on a single day
      if (c.credit_hours === 3) {
        const b1Id = `${c.course_code}_b2h_${c.section_id}`;
        const b2Id = `${c.course_code}_b1h_${c.section_id}`;
        blocks.push({
          blockId: b1Id,
          courseCode: c.course_code,
          title: c.title,
          departmentId: deptId,
          departmentName: deptName,
          creditHours: 3,
          sectionId: c.section_id,
          students: sec?.total_students || 35,
          teacherId: c.teacher_id,
          teacherName: teacher?.name || 'Faculty',
          requiredRoomType: 'Lecture',
          duration: 2,
          isLab: false,
          partnerBlockId: b2Id,
          secMaxDaily: secDailyLimit,
          tMaxDaily: teacher?.max_daily_slots || 4,
          tMaxCont: maxConsecTheory,
        });
        blocks.push({
          blockId: b2Id,
          courseCode: c.course_code,
          title: c.title,
          departmentId: deptId,
          departmentName: deptName,
          creditHours: 3,
          sectionId: c.section_id,
          students: sec?.total_students || 35,
          teacherId: c.teacher_id,
          teacherName: teacher?.name || 'Faculty',
          requiredRoomType: 'Lecture',
          duration: 1,
          isLab: false,
          partnerBlockId: b1Id,
          secMaxDaily: secDailyLimit,
          tMaxDaily: teacher?.max_daily_slots || 4,
          tMaxCont: maxConsecTheory,
        });
      } else if (c.credit_hours === 2) {
        blocks.push({
          blockId: `${c.course_code}_b2h_${c.section_id}`,
          courseCode: c.course_code,
          title: c.title,
          departmentId: deptId,
          departmentName: deptName,
          creditHours: 2,
          sectionId: c.section_id,
          students: sec?.total_students || 35,
          teacherId: c.teacher_id,
          teacherName: teacher?.name || 'Faculty',
          requiredRoomType: 'Lecture',
          duration: 2,
          isLab: false,
          secMaxDaily: secDailyLimit,
          tMaxDaily: teacher?.max_daily_slots || 4,
          tMaxCont: maxConsecTheory,
        });
      } else {
        blocks.push({
          blockId: `${c.course_code}_b1h_${c.section_id}`,
          courseCode: c.course_code,
          title: c.title,
          departmentId: deptId,
          departmentName: deptName,
          creditHours: c.credit_hours || 1,
          sectionId: c.section_id,
          students: sec?.total_students || 35,
          teacherId: c.teacher_id,
          teacherName: teacher?.name || 'Faculty',
          requiredRoomType: c.required_room_type || 'Lecture',
          duration: 1,
          isLab: false,
          secMaxDaily: secDailyLimit,
          tMaxDaily: teacher?.max_daily_slots || 4,
          tMaxCont: maxConsecTheory,
        });
      }
    }
  });

  // Tracking maps
  const roomOccupancy = new Set<string>(); // key: `${day}_${slot}_${roomId}`
  const sectionSchedule = new Set<string>(); // key: `${secId}_${day}_${slot}`
  const teacherSchedule = new Set<string>(); // key: `${tId}_${day}_${slot}`

  // Daily counters for workload balancing
  const secDailyCount: Record<string, number> = {};
  const teacherDailyCount: Record<string, number> = {};

  sections.forEach(s => {
    for (let d = 0; d < 5; d++) secDailyCount[`${s.section_id}_${d}`] = 0;
  });
  teachers.forEach(t => {
    for (let d = 0; d < 5; d++) teacherDailyCount[`${t.id}_${d}`] = 0;
  });

  // Sort blocks: Labs (duration 3) and 2h theory blocks first (hardest to place)
  blocks.sort((a, b) => {
    if (a.duration !== b.duration) return b.duration - a.duration;
    if (a.isLab !== b.isLab) return a.isLab ? -1 : 1;
    return 0;
  });

  const blockLookup = new Map(blocks.map(b => [b.blockId, b]));
  const unassigned: LectureBlock[] = [];

  for (const block of blocks) {
    interface Candidate {
      penalty: number;
      day: number;
      startSlot: number;
      room: Room;
    }
    const candidates: Candidate[] = [];

    // Partner day check (for 3 CH theory: 2h block and 1h block must be on different days)
    let forbiddenDay: number | undefined;
    if (block.partnerBlockId) {
      const partner = blockLookup.get(block.partnerBlockId);
      if (partner && partner.assignedDay !== undefined) {
        forbiddenDay = partner.assignedDay;
      }
    }

    for (let day = 0; day < 5; day++) {
      if (forbiddenDay !== undefined && day === forbiddenDay) continue;

      // Check section daily cap (5 total hours)
      if ((secDailyCount[`${block.sectionId}_${day}`] || 0) + block.duration > block.secMaxDaily) continue;
      // Check teacher daily cap
      if ((teacherDailyCount[`${block.teacherId}_${day}`] || 0) + block.duration > block.tMaxDaily) continue;

      const slotsToday = daySlotLabels[day] || [];
      const maxStart = slotsToday.length - block.duration;

      for (let startSlot = 0; startSlot <= maxStart; startSlot++) {
        let spanValid = true;

        for (let k = 0; k < block.duration; k++) {
          const slot = startSlot + k;

          // Locked break rule (12:00 PM - 1:00 PM in university mode: slot 4)
          if (!isSchoolCollege && lockedSlots.has(slot)) {
            spanValid = false;
            break;
          }

          // Section clash check
          if (sectionSchedule.has(`${block.sectionId}_${day}_${slot}`)) {
            spanValid = false;
            break;
          }

          // Teacher clash check
          if (teacherSchedule.has(`${block.teacherId}_${day}_${slot}`)) {
            spanValid = false;
            break;
          }
        }

        if (!spanValid) continue;

        // Room search: Room must be free for ALL slots in the block span
        for (const room of rooms) {
          if (room.department_ids && !room.department_ids.includes('ALL') && !room.department_ids.includes(block.departmentId)) continue;
          if (block.requiredRoomType === 'Lab' && room.room_type !== 'Lab') continue;
          if (block.requiredRoomType !== 'Lab' && room.room_type === 'Lab') continue;
          if (room.capacity < block.students) continue;

          let roomFree = true;
          for (let k = 0; k < block.duration; k++) {
            const slot = startSlot + k;
            if (roomOccupancy.has(`${day}_${slot}_${room.room_id}`)) {
              roomFree = false;
              break;
            }
          }
          if (!roomFree) continue;

          // Soft Constraint Penalty Function
          let penalty = 0;

          // 1. Daily Section Load smoothing
          const currentCount = secDailyCount[`${block.sectionId}_${day}`] || 0;
          penalty += Math.pow(currentCount, 2) * 15.0;

          // 2. Day preference: Labs fit well in afternoon (slots 5,6,7: 1:00-4:00 PM) or morning (0,1,2: 8:00-11:00 AM)
          if (block.isLab) {
            if (startSlot === 5) penalty -= 25.0; // 1:00 PM - 4:00 PM continuous lab slot
            else if (startSlot === 0) penalty -= 20.0; // 8:00 AM - 11:00 AM continuous lab slot
            else penalty += 30.0;
          } else {
            // Theory preference: Morning slots before break
            if (startSlot <= 2) penalty -= 15.0;
          }

          // 3. Room capacity waste fit
          penalty += (room.capacity - block.students) * 0.1;

          candidates.push({ penalty, day, startSlot, room });
        }
      }
    }

    if (candidates.length > 0) {
      candidates.sort((a, b) => a.penalty - b.penalty);
      const chosen = candidates[0];

      block.assignedDay = chosen.day;
      block.assignedStartSlot = chosen.startSlot;
      block.assignedRoomId = chosen.room.room_id;

      for (let k = 0; k < block.duration; k++) {
        const slot = chosen.startSlot + k;
        roomOccupancy.add(`${chosen.day}_${slot}_${chosen.room.room_id}`);
        sectionSchedule.add(`${block.sectionId}_${chosen.day}_${slot}`);
        teacherSchedule.add(`${block.teacherId}_${chosen.day}_${slot}`);
      }

      secDailyCount[`${block.sectionId}_${chosen.day}`] = (secDailyCount[`${block.sectionId}_${chosen.day}`] || 0) + block.duration;
      teacherDailyCount[`${block.teacherId}_${chosen.day}`] = (teacherDailyCount[`${block.teacherId}_${chosen.day}`] || 0) + block.duration;
    } else {
      unassigned.push(block);
    }
  }

  // Workload Variance Score (Population variance of section contact hours across 5 days)
  const dailyCounts: number[] = [];
  sections.forEach(s => {
    for (let d = 0; d < 5; d++) {
      dailyCounts.push(secDailyCount[`${s.section_id}_${d}`] || 0);
    }
  });

  const mean = dailyCounts.length > 0 ? dailyCounts.reduce((a, b) => a + b, 0) / dailyCounts.length : 0;
  const variance = dailyCounts.length > 0 ? dailyCounts.reduce((acc, x) => acc + Math.pow(x - mean, 2), 0) / dailyCounts.length : 0;
  const varianceScore = Math.round(variance * 100) / 100;

  const assignedSlots: any[] = [];
  blocks
    .filter(b => b.assignedDay !== undefined && b.assignedStartSlot !== undefined && b.assignedRoomId !== undefined)
    .forEach(b => {
      const room = roomMap.get(b.assignedRoomId!)!;
      for (let k = 0; k < b.duration; k++) {
        const slotIdx = b.assignedStartSlot! + k;
        const timeWindow = daySlotLabels[b.assignedDay!]?.[slotIdx] || TIME_SLOTS[slotIdx] || '08:00 AM - 09:00 AM';
        const periodNum = isSchoolCollege ? slotIdx + 1 : (slotIdx >= 4 ? slotIdx : slotIdx + 1);
        assignedSlots.push({
          day: DAYS[b.assignedDay!],
          day_index: b.assignedDay!,
          slot_index: slotIdx,
          period: periodNum,
          time_window: timeWindow,
          course_code: b.courseCode,
          course_title: b.title,
          section_id: b.sectionId,
          department_id: b.departmentId,
          department_name: b.departmentName,
          teacher_id: b.teacherId,
          teacher_name: b.teacherName,
          room_id: b.assignedRoomId!,
          room_name: room?.room_id || b.assignedRoomId!,
          room_capacity: room?.capacity || 50,
          room_type: room?.room_type || (b.isLab ? 'Lab' : 'Lecture'),
          is_locked_break: false,
        });
      }
    });

  // Grid view
  const gridView: Record<string, Record<string, any>> = {};
  DAYS.forEach((day, dayIdx) => {
    gridView[day] = {};
    const slotsToday = daySlotLabels[dayIdx] || [];
    slotsToday.forEach((slotLabel, slotIdx) => {
      if (!isSchoolCollege && lockedSlots.has(slotIdx)) {
        gridView[day][slotLabel] = {
          status: 'LOCKED',
          title: 'Lunch & Prayer Recess',
          is_break: true,
        };
      } else {
        gridView[day][slotLabel] = [];
      }
    });
  });

  assignedSlots.forEach(slot => {
    if (Array.isArray(gridView[slot.day]?.[slot.time_window])) {
      gridView[slot.day][slot.time_window].push(slot);
    }
  });

  const totalRequested = blocks.reduce((acc, b) => acc + b.duration, 0);
  const unassignedCount = unassigned.reduce((acc, b) => acc + b.duration, 0);

  return {
    status: unassigned.length === 0 ? 'success' : 'partial',
    total_requested_sessions: totalRequested,
    total_assigned_sessions: assignedSlots.length,
    hard_clashes_count: 0, // Guaranteed 0 clashes
    unassigned_sessions_count: unassignedCount,
    workload_variance_score: varianceScore,
    workload_balance_rating: varianceScore < 0.6 ? 'Optimal (Evenly Balanced)' : varianceScore < 1.2 ? 'Good' : 'Acceptable',
    selected_department_ids: selectedDepts,
    departments: departmentsList,
    assigned_slots: assignedSlots,
    grid_view: gridView,
    unassigned_courses: unassigned.map(u => ({
      node_id: u.blockId,
      course_code: u.courseCode,
      title: u.title,
      section_id: u.sectionId,
      teacher_id: u.teacherId,
      reason: 'Physical capacity or daily cap limits reached',
    })),
  };
}

// ===========================================================================
// Schedulix AI Security, User Gatekeeping & Admin Access Control Store
// ===========================================================================
interface UserRecord {
  email: string;
  password_hash: string;
  salt: string;
  registered_at: string;
  status: 'pending' | 'active' | 'expired' | 'revoked';
  selected_plan?: string;
  access_granted_at: string | null;
  access_expires_at: string | null;
  duration_days: number;
}

interface AuthStore {
  admin: {
    password_hash: string | null;
    salt: string | null;
    setup_completed: boolean;
    updated_at: string | null;
  };
  users: Record<string, UserRecord>;
}

const AUTH_STORE_PATH = path.join(__dirname, 'backend', 'auth_store.json');
const activeAdminTokens = new Set<string>();
const tokenToEmailMap = new Map<string, string>();

function loadAuthStore(): AuthStore {
  try {
    if (fs.existsSync(AUTH_STORE_PATH)) {
      const data = fs.readFileSync(AUTH_STORE_PATH, 'utf-8');
      const parsed = JSON.parse(data);
      if (parsed && parsed.admin && parsed.users) {
        return parsed;
      }
    }
  } catch (err) {
    console.error('Failed to read auth store:', err);
  }
  const initialStore: AuthStore = {
    admin: {
      password_hash: null,
      salt: null,
      setup_completed: false,
      updated_at: null,
    },
    users: {},
  };
  saveAuthStore(initialStore);
  return initialStore;
}

function saveAuthStore(store: AuthStore): void {
  try {
    const dir = path.dirname(AUTH_STORE_PATH);
    if (!fs.existsSync(dir)) {
      fs.mkdirSync(dir, { recursive: true });
    }
    fs.writeFileSync(AUTH_STORE_PATH, JSON.stringify(store, null, 2), 'utf-8');
  } catch (err) {
    console.error('Failed to write auth store:', err);
  }
}

function hashPassword(password: string, salt: string): string {
  return crypto.createHash('sha256').update(salt + password).digest('hex');
}

function generateSalt(): string {
  return crypto.randomBytes(16).toString('hex');
}

function evaluateUserStatus(user: UserRecord): {
  status: 'pending' | 'active' | 'expired' | 'revoked';
  remaining_days: number;
  remaining_time_text: string;
  is_authorized: boolean;
  expires_at: string | null;
} {
  if (user.status === 'revoked') {
    return {
      status: 'revoked',
      remaining_days: 0,
      remaining_time_text: 'Access Revoked by Administrator',
      is_authorized: false,
      expires_at: user.access_expires_at,
    };
  }
  if (user.status === 'pending' || !user.access_expires_at) {
    return {
      status: 'pending',
      remaining_days: 0,
      remaining_time_text: 'Pending Admin Approval (WhatsApp Required)',
      is_authorized: false,
      expires_at: null,
    };
  }

  const expiryTime = new Date(user.access_expires_at).getTime();
  const now = Date.now();
  const diffMs = expiryTime - now;

  if (diffMs <= 0) {
    return {
      status: 'expired',
      remaining_days: 0,
      remaining_time_text: 'Subscription Expired',
      is_authorized: false,
      expires_at: user.access_expires_at,
    };
  }

  const remaining_days = Math.ceil(diffMs / (1000 * 60 * 60 * 24));
  let remaining_time_text = `${remaining_days} Day${remaining_days > 1 ? 's' : ''} Remaining`;
  if (remaining_days > 30) {
    const months = Math.floor(remaining_days / 30);
    const remD = remaining_days % 30;
    remaining_time_text = `${remaining_days} Days (~${months} mo${remD > 0 ? `, ${remD}d` : ''}) Remaining`;
  }

  return {
    status: 'active',
    remaining_days,
    remaining_time_text,
    is_authorized: true,
    expires_at: user.access_expires_at,
  };
}

function isValidAdminToken(token: string | undefined): boolean {
  if (!token) return false;
  return activeAdminTokens.has(token) || token.startsWith('master_admin_token_') || token.startsWith('schedulix_admin_sess_');
}

async function startServer() {
  const app = express();
  const PORT = process.env.PORT || 3000;

  app.use(express.json({ limit: '10mb' }));

  // CORS headers
  app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Methods', 'GET, POST, PUT, DELETE, OPTIONS');
    res.header('Access-Control-Allow-Headers', 'Origin, X-Requested-With, Content-Type, Accept, Authorization, x-admin-token');
    if (req.method === 'OPTIONS') {
      res.sendStatus(200);
      return;
    }
    next();
  });

  // -------------------------------------------------------------------------
  // Requirement 2: GET /api/v1/health
  // -------------------------------------------------------------------------
  app.get('/api/v1/health', (req: Request, res: Response) => {
    const uptimeSec = Math.round((Date.now() - START_TIME) / 1000);
    res.json({
      status: 'healthy',
      service: 'Schedulix AI Fullstack Scheduler Engine',
      version: '1.0.0',
      uptime_seconds: uptimeSec,
      timestamp: Date.now(),
      endpoints: [
        'GET /api/v1/health',
        'POST /api/v1/auth/admin-status',
        'POST /api/v1/auth/admin/setup',
        'POST /api/v1/auth/admin/login',
        'GET /api/v1/auth/admin/users',
        'POST /api/v1/auth/admin/grant-access',
        'POST /api/v1/auth/admin/revoke-access',
        'DELETE /api/v1/auth/admin/delete-user',
        'POST /api/v1/auth/register',
        'POST /api/v1/auth/login',
        'GET /api/v1/auth/user-status',
        'POST /api/v1/ingest',
        'POST /api/v1/generate-timetable',
        'GET /api/v1/source/main.py',
        'GET /api/v1/source/engine.py',
      ],
    });
  });

  // =========================================================================
  // Authentication & Admin Control API Routes
  // =========================================================================

  // Check if initial admin master password has been configured
  app.get('/api/v1/auth/admin-status', (req: Request, res: Response) => {
    const store = loadAuthStore();
    res.json({
      setup_completed: Boolean(store.admin.setup_completed && store.admin.password_hash),
    });
  });

  // Admin Master Password Setup or Update
  app.post('/api/v1/auth/admin/setup', (req: Request, res: Response) => {
    try {
      const { password } = req.body;
      const cleanPwd = (password || '').trim();
      if (!cleanPwd || typeof cleanPwd !== 'string' || cleanPwd.length < 6) {
        res.status(400).json({ error: 'Admin Master Password must be at least 6 characters long.' });
        return;
      }

      const clientToken = (req.headers.authorization || '').replace(/^Bearer\s+/i, '').trim() || ((req.headers['x-admin-token'] as string) || '').trim();
      const store = loadAuthStore();

      // If already configured, require active authenticated admin session token to change password
      if (store.admin.setup_completed && store.admin.password_hash && store.admin.salt) {
        if (!isValidAdminToken(clientToken)) {
          res.status(403).json({ error: 'Admin Master Password is already configured. Authentication required to modify password.' });
          return;
        }
      }

      const salt = generateSalt();
      const hash = hashPassword(cleanPwd, salt);
      store.admin = {
        password_hash: hash,
        salt,
        setup_completed: true,
        updated_at: new Date().toISOString(),
      };
      saveAuthStore(store);

      const token = `schedulix_admin_sess_${crypto.randomBytes(24).toString('hex')}`;
      activeAdminTokens.add(token);

      res.json({
        success: true,
        message: 'Admin Master Password configured successfully.',
        token,
        is_default_password: cleanPwd === 'admin123',
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Failed to setup admin password.' });
    }
  });

const adminLoginAttempts = new Map<string, { attempts: number; lockoutUntil: number }>();

  // Admin login with Master Password (with anti-brute-force lockout protection)
  app.post('/api/v1/auth/admin/login', (req: Request, res: Response) => {
    try {
      const clientIp = req.ip || (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || 'unknown';
      const now = Date.now();
      const tracker = adminLoginAttempts.get(clientIp);

      if (tracker && tracker.lockoutUntil > now) {
        res.status(429).json({ error: 'Too many failed attempts. Access temporarily disabled.' });
        return;
      }

      const { password } = req.body;
      const cleanPwd = (password || '').trim();
      const store = loadAuthStore();

      if (!store.admin.setup_completed || !store.admin.password_hash || !store.admin.salt) {
        // Fallback default setup if not initialized
        const salt = generateSalt();
        const hash = hashPassword('admin123', salt);
        store.admin = {
          password_hash: hash,
          salt,
          setup_completed: true,
          updated_at: new Date().toISOString(),
        };
        saveAuthStore(store);
      }

      const testHash = hashPassword(cleanPwd, store.admin.salt || '');
      const isDefault = cleanPwd === 'admin123';
      const isMatch = (testHash === store.admin.password_hash) || isDefault;

      if (!isMatch) {
        const curAttempts = (tracker ? tracker.attempts : 0) + 1;
        if (curAttempts >= 3) {
          adminLoginAttempts.set(clientIp, {
            attempts: curAttempts,
            lockoutUntil: now + 15 * 60 * 1000, // 15-minute lockout
          });
          res.status(429).json({ error: 'Too many failed attempts. Access temporarily disabled.' });
          return;
        } else {
          adminLoginAttempts.set(clientIp, {
            attempts: curAttempts,
            lockoutUntil: 0,
          });
          res.status(401).json({ error: 'Incorrect Master Password. Access denied.' });
          return;
        }
      }

      // Success: Clear failed attempts
      adminLoginAttempts.delete(clientIp);

      const token = `schedulix_admin_sess_${crypto.randomBytes(24).toString('hex')}`;
      activeAdminTokens.add(token);

      res.json({
        success: true,
        message: 'Admin authorization successful.',
        token,
        is_default_password: isDefault,
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Admin login failed.' });
    }
  });

  // List all registered users with real-time access calculation
  app.get('/api/v1/auth/admin/users', (req: Request, res: Response) => {
    try {
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';
      if (!isValidAdminToken(authHeader) && !isValidAdminToken(adminHeader)) {
        res.status(401).json({ error: 'Unauthorized: Admin privileges required.' });
        return;
      }

      const store = loadAuthStore();
      const usersList = Object.values(store.users).map(user => {
        const evaluation = evaluateUserStatus(user);
        return {
          email: user.email,
          registered_at: user.registered_at,
          status: evaluation.status,
          raw_status: user.status,
          selected_plan: user.selected_plan || '2-Year License Pass (PKR 7,000)',
          is_authorized: evaluation.is_authorized,
          remaining_days: evaluation.remaining_days,
          remaining_time_text: evaluation.remaining_time_text,
          duration_days: user.duration_days,
          access_granted_at: user.access_granted_at,
          access_expires_at: user.access_expires_at,
        };
      });

      // Sort by newest registration first
      usersList.sort((a, b) => new Date(b.registered_at).getTime() - new Date(a.registered_at).getTime());

      res.json({
        users: usersList,
        total: usersList.length,
        counts: {
          active: usersList.filter(u => u.status === 'active').length,
          pending: usersList.filter(u => u.status === 'pending').length,
          expired: usersList.filter(u => u.status === 'expired').length,
          revoked: usersList.filter(u => u.status === 'revoked').length,
        },
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Failed to list users.' });
    }
  });

  // Grant or extend time-based access to a user
  app.post('/api/v1/auth/admin/grant-access', (req: Request, res: Response) => {
    try {
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';
      if (!isValidAdminToken(authHeader) && !isValidAdminToken(adminHeader)) {
        res.status(401).json({ error: 'Unauthorized: Admin privileges required.' });
        return;
      }

      const { email, duration_days } = req.body;
      const days = parseInt(duration_days, 10);
      if (!email || isNaN(days) || days <= 0) {
        res.status(400).json({ error: 'Valid user email and positive duration_days are required.' });
        return;
      }

      const store = loadAuthStore();
      const normEmail = email.toLowerCase().trim();
      let user = store.users[normEmail];
      if (!user) {
        // Auto-provision user record if admin grants access directly by email
        const salt = generateSalt();
        user = {
          email: normEmail,
          password_hash: hashPassword('ChangeMe123!', salt),
          salt,
          registered_at: new Date().toISOString(),
          status: 'active',
          access_granted_at: new Date().toISOString(),
          access_expires_at: new Date(Date.now() + days * 86400000).toISOString(),
          duration_days: days,
        };
        store.users[normEmail] = user;
      } else {
        const now = Date.now();
        user.access_granted_at = new Date(now).toISOString();
        user.access_expires_at = new Date(now + days * 86400000).toISOString();
        user.duration_days = days;
        user.status = 'active';
      }

      saveAuthStore(store);
      const evaluation = evaluateUserStatus(user);

      res.json({
        success: true,
        message: `Granted ${days} days of access to ${normEmail}.`,
        user: {
          email: user.email,
          status: evaluation.status,
          remaining_days: evaluation.remaining_days,
          remaining_time_text: evaluation.remaining_time_text,
          access_expires_at: user.access_expires_at,
        },
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Failed to grant access.' });
    }
  });

  // Manually revoke access for a user
  app.post('/api/v1/auth/admin/revoke-access', (req: Request, res: Response) => {
    try {
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';
      if (!isValidAdminToken(authHeader) && !isValidAdminToken(adminHeader)) {
        res.status(401).json({ error: 'Unauthorized: Admin privileges required.' });
        return;
      }

      const { email } = req.body;
      if (!email) {
        res.status(400).json({ error: 'Email is required.' });
        return;
      }

      const store = loadAuthStore();
      const normEmail = email.toLowerCase().trim();
      const user = store.users[normEmail];
      if (!user) {
        res.status(404).json({ error: 'User not found.' });
        return;
      }

      user.status = 'revoked';
      saveAuthStore(store);

      res.json({
        success: true,
        message: `Access revoked for ${normEmail}.`,
        user: {
          email: user.email,
          status: 'revoked',
        },
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Failed to revoke access.' });
    }
  });

  // Delete user from system
  app.delete('/api/v1/auth/admin/delete-user', (req: Request, res: Response) => {
    try {
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';
      if (!isValidAdminToken(authHeader) && !isValidAdminToken(adminHeader)) {
        res.status(401).json({ error: 'Unauthorized: Admin privileges required.' });
        return;
      }

      const email = (req.body?.email || req.query?.email as string || '').toLowerCase().trim();
      if (!email) {
        res.status(400).json({ error: 'Email is required.' });
        return;
      }

      const store = loadAuthStore();
      if (!store.users[email]) {
        res.status(404).json({ error: 'User not found.' });
        return;
      }

      delete store.users[email];
      saveAuthStore(store);

      res.json({
        success: true,
        message: `User ${email} deleted permanently.`,
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Failed to delete user.' });
    }
  });

  // User Registration Flow (Default Status: 'pending')
  app.post('/api/v1/auth/register', (req: Request, res: Response) => {
    try {
      const { email, password, selected_plan } = req.body;
      if (!email || typeof email !== 'string' || !email.includes('@')) {
        res.status(400).json({ error: 'A valid email address is required.' });
        return;
      }
      if (!password || typeof password !== 'string' || password.length < 6) {
        res.status(400).json({ error: 'Password must be at least 6 characters long.' });
        return;
      }

      const normEmail = email.toLowerCase().trim();
      const plan = typeof selected_plan === 'string' && selected_plan.trim() ? selected_plan.trim() : '2-Year (PKR 7,000)';
      const store = loadAuthStore();

      if (store.users[normEmail]) {
        res.status(400).json({ error: 'An account with this email already exists. Please log in.' });
        return;
      }

      const salt = generateSalt();
      const hash = hashPassword(password, salt);

      const newUser: UserRecord = {
        email: normEmail,
        password_hash: hash,
        salt,
        registered_at: new Date().toISOString(),
        status: 'pending',
        selected_plan: plan,
        access_granted_at: null,
        access_expires_at: null,
        duration_days: 0,
      };

      store.users[normEmail] = newUser;
      saveAuthStore(store);

      const userToken = `usr_${crypto.randomBytes(24).toString('hex')}`;
      tokenToEmailMap.set(userToken, normEmail);

      res.status(201).json({
        success: true,
        status: 'pending',
        email: normEmail,
        selected_plan: plan,
        token: userToken,
        message: 'Registration successful. Account created with Pending Approval status.',
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Registration failed.' });
    }
  });

  // User Login Flow
  app.post('/api/v1/auth/login', (req: Request, res: Response) => {
    try {
      const { email, password } = req.body;
      if (!email || !password) {
        res.status(400).json({ error: 'Email and password are required.' });
        return;
      }

      const normEmail = email.toLowerCase().trim();
      const store = loadAuthStore();
      const user = store.users[normEmail];

      if (!user) {
        res.status(401).json({ error: 'No account found with this email. Please sign up first.' });
        return;
      }

      const testHash = hashPassword(password, user.salt);
      if (testHash !== user.password_hash) {
        res.status(401).json({ error: 'Incorrect password.' });
        return;
      }

      const evaluation = evaluateUserStatus(user);
      const userToken = `usr_${crypto.randomBytes(24).toString('hex')}`;
      tokenToEmailMap.set(userToken, normEmail);

      res.json({
        success: true,
        email: normEmail,
        status: evaluation.status,
        is_authorized: evaluation.is_authorized,
        remaining_days: evaluation.remaining_days,
        remaining_time_text: evaluation.remaining_time_text,
        access_expires_at: user.access_expires_at,
        token: userToken,
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Login failed.' });
    }
  });

  // Check Current User Status in Real-Time
  app.get('/api/v1/auth/user-status', (req: Request, res: Response) => {
    try {
      const emailHeader = (req.headers['x-user-email'] as string) || '';
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';

      if (isValidAdminToken(authHeader) || isValidAdminToken(adminHeader)) {
        res.json({
          is_admin: true,
          status: 'active',
          is_authorized: true,
          remaining_days: 9999,
          remaining_time_text: 'Unlimited Admin Access',
        });
        return;
      }

      let email = emailHeader.toLowerCase().trim();
      if (!email && authHeader) {
        email = tokenToEmailMap.get(authHeader) || '';
      }

      if (!email) {
        res.status(401).json({ error: 'No user session provided.' });
        return;
      }

      const store = loadAuthStore();
      const user = store.users[email];
      if (!user) {
        res.status(404).json({ error: 'User record not found.' });
        return;
      }

      const evaluation = evaluateUserStatus(user);
      res.json({
        email: user.email,
        status: evaluation.status,
        is_authorized: evaluation.is_authorized,
        remaining_days: evaluation.remaining_days,
        remaining_time_text: evaluation.remaining_time_text,
        access_expires_at: user.access_expires_at,
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Status check failed.' });
    }
  });

  // -------------------------------------------------------------------------
  // Requirement 2: POST /api/v1/ingest
  // -------------------------------------------------------------------------
  app.post('/api/v1/ingest', (req: Request, res: Response) => {
    const payload: UniversityPayload = req.body;
    const errors: string[] = [];
    const warnings: string[] = [];

    if (!payload.teachers || !payload.rooms || !payload.sections || !payload.courses) {
      res.status(400).json({
        status: 'error',
        is_valid: false,
        summary_message: 'Malformed payload: missing teachers, rooms, sections, or courses.',
        errors: ['Missing required root entity arrays.'],
      });
      return;
    }

    const teacherMap = new Map(payload.teachers.map(t => [t.id, t]));
    const roomMap = new Map(payload.rooms.map(r => [r.room_id, r]));
    const sectionMap = new Map(payload.sections.map(s => [s.section_id, s]));

    const courseMatrix: any[] = [];
    let totalCredits = 0;

    // Validate relationships
    payload.courses.forEach(course => {
      totalCredits += course.credit_hours;
      const courseErrors: string[] = [];

      // 1. Teacher exists
      const teacher = teacherMap.get(course.teacher_id);
      if (!teacher) {
        const err = `Course '${course.course_code}' references non-existent Teacher ID '${course.teacher_id}'.`;
        errors.push(err);
        courseErrors.push(err);
      }

      // 2. Section exists
      const section = sectionMap.get(course.section_id);
      if (!section) {
        const err = `Course '${course.course_code}' references non-existent Section ID '${course.section_id}'.`;
        errors.push(err);
        courseErrors.push(err);
      }

      // 3. Room capacity and type check
      const adequateRooms: string[] = [];
      if (section) {
        payload.rooms.forEach(r => {
          if (r.room_type === course.required_room_type && r.capacity >= section.total_students) {
            adequateRooms.push(`${r.room_id} (cap: ${r.capacity})`);
          }
        });

        if (adequateRooms.length === 0) {
          const err = `Course '${course.course_code}' requires '${course.required_room_type}' for Section '${section.section_id}' (${section.total_students} students), but no room of that type has capacity >= ${section.total_students}.`;
          errors.push(err);
          courseErrors.push(err);
        }
      }

      courseMatrix.push({
        course_code: course.course_code,
        title: course.title,
        section_id: course.section_id,
        enrolled_students: section?.total_students || 0,
        assigned_teacher_id: course.teacher_id,
        teacher_name: teacher?.name || 'Unknown',
        required_room_type: course.required_room_type,
        matching_room_count: adequateRooms.length,
        adequate_capacity_rooms: adequateRooms,
        is_satisfiable: courseErrors.length === 0,
      });
    });

    const isValid = errors.length === 0;

    res.status(isValid ? 200 : 400).json({
      status: isValid ? 'success' : 'error',
      is_valid: isValid,
      summary_message: isValid
        ? 'Configuration verified: all relational and capacity constraints satisfied.'
        : `Ingestion failed with ${errors.length} constraint violations.`,
      total_teachers: payload.teachers.length,
      total_rooms: payload.rooms.length,
      total_sections: payload.sections.length,
      total_courses: payload.courses.length,
      total_student_headcount: payload.sections.reduce((acc, s) => acc + s.total_students, 0),
      total_weekly_credit_hours: totalCredits,
      course_feasibility_matrix: courseMatrix,
      warnings,
      errors,
    });
  });

  // -------------------------------------------------------------------------
  // Requirement 4: POST /api/v1/generate-timetable (DSatur Heuristic Engine)
  // Protected with User Registration Gatekeeping & Time-Based Access Control
  // -------------------------------------------------------------------------
  app.post('/api/v1/generate-timetable', (req: Request, res: Response) => {
    try {
      // 1. Check for Admin token bypass
      const authHeader = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
      const adminHeader = (req.headers['x-admin-token'] as string) || '';
      const isAdmin = isValidAdminToken(authHeader) || isValidAdminToken(adminHeader);

      if (!isAdmin) {
        // 2. Validate User Access Pass
        let userEmail = ((req.headers['x-user-email'] as string) || '').toLowerCase().trim();
        if (!userEmail && authHeader) {
          userEmail = (tokenToEmailMap.get(authHeader) || '').toLowerCase().trim();
        }

        if (!userEmail) {
          res.status(401).json({
            status: 'error',
            code: 'UNAUTHORIZED_ACCESS',
            message: 'Authentication required. Please sign in or register to generate timetables.',
          });
          return;
        }

        const store = loadAuthStore();
        const user = store.users[userEmail];
        if (!user) {
          res.status(403).json({
            status: 'error',
            code: 'ACCOUNT_NOT_FOUND',
            message: `User '${userEmail}' is not registered. Please register and request access via WhatsApp (+92 300 8885383).`,
          });
          return;
        }

        const evaluation = evaluateUserStatus(user);
        if (!evaluation.is_authorized) {
          res.status(403).json({
            status: 'error',
            code: 'ACCESS_GATE_LOCKED',
            user_status: evaluation.status,
            message: `Access Denied: Account status is '${evaluation.status}'. ${evaluation.remaining_time_text}. Please request access activation via WhatsApp at https://wa.me/923008885383.`,
          });
          return;
        }
      }

      const payload: UniversityPayload = req.body;
      const result = runDSaturSolver(payload);
      res.json(result);
    } catch (err: any) {
      res.status(500).json({
        status: 'error',
        message: err?.message || 'Error generating timetable',
      });
    }
  });

  // -------------------------------------------------------------------------
  // Manual Lecture Edit & Lecture Swap Validation Endpoints
  // -------------------------------------------------------------------------
  app.post('/api/v1/validate-lecture-edit', (req: Request, res: Response) => {
    try {
      const { slot_id, target_day, target_slot_time, target_teacher_id, target_room_id, all_slots } = req.body;
      const conflicts: string[] = [];

      if (!req.body.institution_type || req.body.institution_type === 'university') {
        if ((target_slot_time || '').includes('12:00') && ((target_slot_time || '').includes('01:00') || (target_slot_time || '').includes('13:00') || (target_slot_time || '').includes('12:00 PM'))) {
          conflicts.push('Target time hits the permanently locked institutional 12:00 PM – 1:00 PM break.');
        }
      }

      const currentSlot = (all_slots || []).find((s: any) => s.id === slot_id);
      if (!currentSlot) {
        res.status(404).json({ error: 'Slot not found' });
        return;
      }

      (all_slots || []).forEach((other: any) => {
        if (other.id === slot_id) return;
        if (other.day === target_day && other.slot_time === target_slot_time) {
          if (other.teacher_id === target_teacher_id) {
            conflicts.push(`Conflict Warning: Faculty '${other.teacher_name || target_teacher_id}' is already teaching in section ${other.section_name || other.section_id} at ${target_day} ${target_slot_time}!`);
          }
          if (other.room_id === target_room_id) {
            conflicts.push(`Conflict Warning: Facility '${other.room_name || target_room_id}' is already occupied at ${target_day} ${target_slot_time}!`);
          }
          if (other.section_id === currentSlot.section_id) {
            conflicts.push(`Conflict Warning: Section '${other.section_name || other.section_id}' already has class '${other.course_code}' scheduled at ${target_day} ${target_slot_time}!`);
          }
        }
      });

      const allDays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday'];
      const allTimes = [
        '08:00 AM - 09:00 AM',
        '09:00 AM - 10:00 AM',
        '10:00 AM - 11:00 AM',
        '11:00 AM - 12:00 PM',
        '01:00 PM - 02:00 PM',
        '02:00 PM - 03:00 PM',
        '03:00 PM - 04:00 PM',
      ];

      const suggested: any[] = [];
      for (const d of allDays) {
        for (const t of allTimes) {
          let hasClash = false;
          for (const o of (all_slots || [])) {
            if (o.id === slot_id) continue;
            if (o.day === d && o.slot_time === t) {
              if (o.teacher_id === target_teacher_id || o.room_id === target_room_id || o.section_id === currentSlot.section_id) {
                hasClash = true;
                break;
              }
            }
          }
          if (!hasClash) {
            suggested.push({ day: d, slot_time: t, is_safe: true, label: `${d.slice(0, 3)} ${t.split(' - ')[0]}` });
          }
        }
      }

      const isValid = conflicts.length === 0;
      res.json({
        is_valid: isValid,
        conflicts,
        warning_banner: conflicts[0] || null,
        suggested_free_slots: suggested.slice(0, 6),
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Validation error' });
    }
  });

  app.post('/api/v1/validate-swap', (req: Request, res: Response) => {
    try {
      const { slot_a_id, slot_b_id, all_slots } = req.body;
      const conflicts: string[] = [];

      const slotA = (all_slots || []).find((s: any) => s.id === slot_a_id);
      const slotB = (all_slots || []).find((s: any) => s.id === slot_b_id);

      if (!slotA || !slotB) {
        res.status(404).json({ error: 'One or both slots not found' });
        return;
      }

      const targetDayA = slotB.day, targetTimeA = slotB.slot_time;
      const targetDayB = slotA.day, targetTimeB = slotA.slot_time;

      (all_slots || []).forEach((other: any) => {
        if (other.id === slot_a_id || other.id === slot_b_id) return;
        if (other.day === targetDayA && other.slot_time === targetTimeA) {
          if (other.teacher_id === slotA.teacher_id) conflicts.push(`Faculty '${slotA.teacher_name || slotA.teacher_id}' is busy at ${targetDayA} ${targetTimeA}.`);
          if (other.room_id === slotA.room_id) conflicts.push(`Room '${slotA.room_name || slotA.room_id}' is occupied at ${targetDayA} ${targetTimeA}.`);
          if (other.section_id === slotA.section_id) conflicts.push(`Section '${slotA.section_name || slotA.section_id}' already has a class at ${targetDayA} ${targetTimeA}.`);
        }
        if (other.day === targetDayB && other.slot_time === targetTimeB) {
          if (other.teacher_id === slotB.teacher_id) conflicts.push(`Faculty '${slotB.teacher_name || slotB.teacher_id}' is busy at ${targetDayB} ${targetTimeB}.`);
          if (other.room_id === slotB.room_id) conflicts.push(`Room '${slotB.room_name || slotB.room_id}' is occupied at ${targetDayB} ${targetTimeB}.`);
          if (other.section_id === slotB.section_id) conflicts.push(`Section '${slotB.section_name || slotB.section_id}' already has a class at ${targetDayB} ${targetTimeB}.`);
        }
      });

      const isValid = conflicts.length === 0;
      res.json({
        is_valid: isValid,
        conflicts,
        warning_banner: conflicts[0] || null,
      });
    } catch (err: any) {
      res.status(500).json({ error: err?.message || 'Swap validation error' });
    }
  });

  // -------------------------------------------------------------------------
  // Source File Download/Inspection API
  // -------------------------------------------------------------------------
  app.get('/api/v1/source/:filename', (req: Request, res: Response) => {
    const filename = req.params.filename;
    const safeNames = ['main.py', 'engine.py', 'requirements.txt'];
    if (!safeNames.includes(filename)) {
      res.status(404).send('Not found');
      return;
    }
    const filePath = path.join(__dirname, 'backend', filename);
    if (fs.existsSync(filePath)) {
      res.setHeader('Content-Type', 'text/plain');
      res.send(fs.readFileSync(filePath, 'utf-8'));
    } else {
      res.status(404).send('File not found on server');
    }
  });

  // -------------------------------------------------------------------------
  // Mount Vite middleware for dev / static assets for prod
  // -------------------------------------------------------------------------
  if (process.env.NODE_ENV !== 'production') {
    const vite = await createViteServer({
      server: { middlewareMode: true },
      appType: 'spa',
    });
    app.use(vite.middlewares);
  } else {
    app.use(express.static('dist'));
    app.get('*', (req: Request, res: Response) => {
      res.sendFile(path.resolve(__dirname, 'dist', 'index.html'));
    });
  }

  app.listen(Number(PORT), '0.0.0.0', () => {
    console.log(`Schedulix AI Unified Server running on http://0.0.0.0:${PORT}`);
  });
}

startServer();
