import json
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

import instructor_lms


class InstructorLmsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        patches = (
            mock.patch.object(instructor_lms, "DB_PATH", root / "lms.sqlite3"),
            mock.patch.object(instructor_lms, "TRANSCRIPTS_DIR", root / "transcripts"),
            mock.patch.object(instructor_lms, "SUMMARIES_DIR", root / "summaries"),
            mock.patch.object(instructor_lms, "VIDEO_DIR", root / "videos"),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        secret_patch = mock.patch.dict(
            os.environ, {"LECTURESEEK_JWT_SECRET": "test-only-secret-key-with-at-least-32-bytes"},
        )
        secret_patch.start()
        self.addCleanup(secret_patch.stop)
        self.addCleanup(instructor_lms.dispose_database, root / "lms.sqlite3")
        instructor_lms.init_db()
        app = FastAPI()
        app.middleware("http")(instructor_lms.protect_lms_video_resources)
        app.include_router(instructor_lms.router)
        self.instructor = TestClient(app)
        self.addCleanup(self.instructor.close)
        self.student = TestClient(app)
        self.addCleanup(self.student.close)
        self.assertIn("LectureSeek · Learning space", self.instructor.get("/instructor").text)
        self.assertIn("LectureSeek · Learning space", self.instructor.get("/student").text)
        self.assertTrue(self.instructor.get("/api/lms/session").json()["setup_required"])
        setup = self.instructor.post("/api/lms/auth/setup", json={
            "name": "Avery Instructor", "email": "avery@example.com",
            "password": "instructor-password-123",
        })
        self.assertEqual(setup.status_code, 200, setup.text)
        access_token = setup.json()["access_token"]
        self.assertEqual(access_token.count("."), 2)
        self.assertEqual(setup.json()["token_type"], "bearer")
        bearer = TestClient(app)
        self.addCleanup(bearer.close)
        bearer_login = bearer.post("/api/lms/auth/login", json={
            "email": "avery@example.com", "password": "instructor-password-123",
        })
        self.assertEqual(bearer_login.status_code, 200, bearer_login.text)
        access_token = bearer_login.json()["access_token"]
        bearer.cookies.clear()
        self.assertEqual(bearer.get(
            "/api/lms/session", headers={"Authorization": f"Bearer {access_token}"},
        ).json()["user"]["role"], "instructor")
        invalid_bearer = TestClient(app)
        self.addCleanup(invalid_bearer.close)
        self.assertEqual(invalid_bearer.get(
            "/api/lms/courses", headers={"Authorization": f"Bearer {access_token}x"},
        ).status_code, 401)
        self.assertEqual(bearer.post(
            "/api/lms/auth/logout", headers={"Authorization": f"Bearer {access_token}"},
        ).status_code, 200)
        self.assertEqual(bearer.get(
            "/api/lms/courses", headers={"Authorization": f"Bearer {access_token}"},
        ).status_code, 401)
        self.course = self.instructor.post("/api/lms/courses", json={
            "title": "Foundations", "description": "An introductory course",
        }).json()
        self.student_account = self.instructor.post("/api/lms/students", json={
            "name": "Sam Student", "email": "sam@example.com", "password": "student-password-123",
        }).json()["student"]
        self.assertEqual(self.instructor.post(
            f"/api/lms/courses/{self.course['id']}/enrollments",
            json={"student_id": self.student_account["id"]},
        ).status_code, 200)
        login = self.student.post("/api/lms/auth/login", json={
            "email": "sam@example.com", "password": "student-password-123",
        })
        self.assertEqual(login.status_code, 200, login.text)

    def test_role_registration_and_instructor_invitation(self):
        instructor_counts = self.instructor.get("/api/lms/dashboard").json()["counts"]
        self.assertEqual(instructor_counts, {
            "courses": 1, "students": 1, "lectures": 0, "pending_submissions": 0,
        })
        student_signup = TestClient(self.instructor.app)
        self.addCleanup(student_signup.close)
        response = student_signup.post("/api/lms/auth/register", json={
            "name": "Taylor Learner", "email": "taylor@example.com",
            "password": "student-password-456", "role": "student",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["role"], "student")
        self.assertEqual(student_signup.get("/api/lms/session").json()["user"]["email"], "taylor@example.com")
        self.assertEqual(student_signup.get("/api/lms/dashboard").json()["counts"]["courses"], 0)

        self.assertEqual(student_signup.post("/api/lms/auth/register", json={
            "name": "Blocked Instructor", "email": "blocked@example.com",
            "password": "instructor-password-789", "role": "instructor",
        }).status_code, 403)
        self.assertEqual(student_signup.post("/api/lms/auth/register", json={
            "name": "Invalid Role", "email": "invalid@example.com",
            "password": "student-password-456", "role": "admin",
        }).status_code, 400)

        invited = self.instructor.post("/api/lms/instructors", json={
            "name": "Morgan Instructor", "email": "morgan@example.com",
            "password": "instructor-password-456",
        })
        self.assertEqual(invited.status_code, 200, invited.text)
        self.assertEqual(invited.json()["instructor"]["role"], "instructor")
        self.assertEqual(self.instructor.get("/api/lms/instructors").status_code, 200)
        self.assertEqual(student_signup.get("/api/lms/instructors").status_code, 403)
        duplicate = self.instructor.post("/api/lms/instructors", json={
            "name": "Duplicate", "email": "morgan@example.com",
            "password": "instructor-password-456",
        })
        self.assertEqual(duplicate.status_code, 409)

    def test_authentication_requires_configured_jwt_secret(self):
        client = TestClient(self.instructor.app)
        self.addCleanup(client.close)
        with mock.patch.dict(os.environ, {"LECTURESEEK_JWT_SECRET": ""}):
            response = client.post("/api/lms/auth/register", json={
                "name": "Unconfigured account", "email": "unconfigured@example.com",
                "password": "student-password-123", "role": "student",
            })
        self.assertEqual(response.status_code, 503)
        with instructor_lms._connect() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM users WHERE email='unconfigured@example.com'",
            ).fetchone())

    def test_lecture_seek_brand_links_return_to_home(self):
        templates = Path(instructor_lms.__file__).resolve().parent / "templates"
        for filename in ("home.html", "index.html", "instructor.html"):
            html = (templates / filename).read_text(encoding="utf-8")
            self.assertIn('href="/"', html, filename)

    def test_auth_roles_course_privacy_and_student_published_content(self):
        anonymous = TestClient(self.instructor.app)
        anonymous.cookies.clear()
        self.assertEqual(anonymous.get("/api/lms/courses").status_code, 401)
        self.assertEqual(self.student.post("/api/lms/courses", json={"title": "Nope"}).status_code, 403)

        lecture_id = "lecture_private"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], None, "Draft lecture", "mediahash0001", None,
                "draft", json.dumps({"transcript": [{"start": 0, "text": "Draft"}]}), None,
                instructor_lms._now(),
            ))
        module = self.instructor.post(
            f"/api/lms/courses/{self.course['id']}/modules", json={"title": "Week one"},
        ).json()
        resource = self.instructor.post(
            f"/api/lms/courses/{self.course['id']}/resources",
            json={"title": "Reading", "url": "https://example.com/reading", "module_id": module["id"]},
        ).json()
        self.assertEqual(module["status"], "draft")
        self.assertEqual(resource["status"], "draft")
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        student_course = self.student.get(f"/api/lms/courses/{self.course['id']}")
        self.assertEqual(student_course.status_code, 200)
        self.assertEqual(student_course.json()["lectures"], [])
        self.assertEqual(student_course.json()["modules"], [])
        self.assertEqual(student_course.json()["resources"], [])
        self.assertEqual(self.student.get(f"/api/lms/lectures/{lecture_id}").status_code, 404)
        self.assertFalse(instructor_lms.legacy_video_access_allowed(
            self.student_request(), "mediahash0001",
        ))
        self.assertEqual(self.student.get(
            "/static/transcripts/mediahash0001_whisper.json",
        ).status_code, 403)

        other = self.instructor.post("/api/lms/courses", json={"title": "Private course"}).json()
        self.assertEqual(self.student.get(f"/api/lms/courses/{other['id']}").status_code, 404)

        self.instructor.put(f"/api/lms/modules/{module['id']}", json={
            "title": "Week one", "status": "published",
        })
        self.instructor.put(f"/api/lms/resources/{resource['id']}", json={
            "title": "Reading", "url": "https://example.com/reading",
            "module_id": module["id"], "status": "published",
        })
        visible = self.student.get(f"/api/lms/courses/{self.course['id']}").json()
        self.assertEqual(visible["modules"][0]["id"], module["id"])
        self.assertEqual(visible["resources"][0]["id"], resource["id"])

        self.instructor.post(f"/api/lms/lectures/{lecture_id}/approve")
        self.instructor.post(f"/api/lms/lectures/{lecture_id}/publish")
        self.assertTrue(instructor_lms.legacy_video_access_allowed(
            self.student_request(), "mediahash0001",
        ))
        self.assertEqual(self.student.get(f"/api/lms/lectures/{lecture_id}").status_code, 200)

    def test_private_course_share_link_and_public_enrollment(self):
        private = self.instructor.post("/api/lms/courses", json={
            "title": "Invite only", "visibility": "private",
        }).json()
        self.instructor.put(f"/api/lms/courses/{private['id']}", json={
            "title": private["title"], "description": "", "status": "published", "visibility": "private",
        })
        self.assertTrue(private["share_token"])
        self.assertEqual(self.student.get(f"/api/lms/courses/{private['id']}").status_code, 404)
        enrolled = self.student.post(f"/api/lms/enrollments/by-link/{private['share_token']}")
        self.assertEqual(enrolled.status_code, 200, enrolled.text)
        self.assertEqual(enrolled.json()["course_id"], private["id"])
        course_details = self.student.get(f"/api/lms/courses/{private['id']}").json()
        self.assertEqual(course_details["course"]["id"], private["id"])
        self.assertNotIn("share_token", course_details["course"])
        self.assertEqual(self.student.post(f"/api/lms/enrollments/by-link/{private['share_token']}").status_code, 409)

        public = self.instructor.post("/api/lms/courses", json={
            "title": "Open course", "visibility": "public",
        }).json()
        self.instructor.put(f"/api/lms/courses/{public['id']}", json={
            "title": public["title"], "description": "", "status": "published", "visibility": "public",
        })
        catalog = self.student.get("/api/lms/public-courses")
        self.assertEqual(catalog.status_code, 200)
        self.assertIn(public["id"], [item["id"] for item in catalog.json()["courses"]])
        self.assertNotIn(private["id"], [item["id"] for item in catalog.json()["courses"]])
        self.assertEqual(self.student.post(f"/api/lms/public-courses/{public['id']}/enroll").status_code, 200)
        self.assertEqual(self.student.post(f"/api/lms/public-courses/{public['id']}/enroll").status_code, 409)

    def test_quiz_submission_grading_and_answer_key_privacy(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        created = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Check-in", "questions": [
                {"prompt": "Which is correct?", "kind": "multiple_choice",
                 "options": ["Right", "Wrong"], "answer": "Right", "points": 5},
                {"prompt": "True or false?", "kind": "true_false",
                 "options": ["True", "False"], "answer": "False", "points": 2},
            ],
        })
        self.assertEqual(created.status_code, 200, created.text)
        quiz_id = created.json()["id"]
        published = self.instructor.put(f"/api/lms/quizzes/{quiz_id}", json={
            "title": "Check-in", "status": "published",
        })
        self.assertEqual(published.status_code, 200, published.text)
        self.assertEqual(published.json()["attempt_limit"], 1)
        edited = self.instructor.put(f"/api/lms/quizzes/{quiz_id}", json={
            "title": "Check-in updated", "status": "published", "questions": [
                {"prompt": "Updated question?", "kind": "multiple_choice",
                 "options": ["Right", "Wrong"], "answer": "Right", "points": 5},
                {"prompt": "True or false?", "kind": "true_false", "answer": "False", "points": 2},
            ],
        })
        self.assertEqual(edited.status_code, 200, edited.text)

        quiz = self.student.get(f"/api/lms/students/quizzes/{quiz_id}").json()
        self.assertTrue(all("answer" not in question for question in quiz["questions"]))
        answers = {question["id"]: ("Right" if index == 0 else "False")
                   for index, question in enumerate(quiz["questions"])}
        submitted = self.student.post(f"/api/lms/quizzes/{quiz_id}/submit", json={"answers": answers})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["grade"], 7)
        self.assertEqual(submitted.json()["status"], "submitted")
        self.assertEqual(self.student.post(
            f"/api/lms/quizzes/{quiz_id}/submit", json={"answers": answers},
        ).status_code, 409)

        rows = self.instructor.get(f"/api/lms/courses/{self.course['id']}/submissions").json()["submissions"]
        self.assertEqual(rows[0]["max_grade"], 7)
        self.assertEqual(self.instructor.put(
            f"/api/lms/submissions/{rows[0]['id']}/grade",
            json={"grade": 6, "feedback": "Good work"},
        ).status_code, 200)
        course_totals = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/gradebook",
        ).json()["totals"][0]
        self.assertEqual(course_totals["earned_points"], 6)
        self.assertEqual(course_totals["available_points"], 7)
        self.assertEqual(course_totals["percentage"], 85.7)
        self.assertEqual(self.instructor.put(
            f"/api/lms/submissions/{rows[0]['id']}/grade",
            json={"grade": 8, "feedback": "Over maximum"},
        ).status_code, 400)
        self.assertEqual(self.student.put(
            f"/api/lms/submissions/{rows[0]['id']}/grade",
            json={"grade": 7, "feedback": "Not allowed"},
        ).status_code, 403)
        grades = self.student.get(f"/api/lms/students/courses/{self.course['id']}/gradebook").json()["grades"]
        self.assertEqual(grades[0]["grade"], 6)
        question_ids = [question["id"] for question in quiz["questions"]]
        with instructor_lms._connect() as db:
            for index in range(4):
                student_id = f"analytics_student_{index}"
                db.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (
                    student_id, f"Analytics Student {index}", f"analytics{index}@example.com",
                    "test-hash", "student", instructor_lms._now(),
                ))
                db.execute("INSERT INTO enrollments VALUES(?,?,?,?)", (
                    f"analytics_enrollment_{index}", self.course["id"], student_id, instructor_lms._now(),
                ))
                db.execute(
                    """INSERT INTO submissions
                       (id,quiz_id,student_id,answers_json,grade,feedback,submitted_at,started_at,
                        attempt_number,status,grading_status)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"analytics_submission_{index}", quiz_id, student_id,
                     json.dumps({question_ids[0]: "Wrong", question_ids[1]: "True"}),
                     0, "", instructor_lms._now(), instructor_lms._now(), 1, "submitted", "graded"),
                )
            db.execute(
                """INSERT INTO submissions
                   (id,quiz_id,student_id,answers_json,grade,feedback,submitted_at,started_at,
                    attempt_number,status,grading_status)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("analytics_latest_submission", quiz_id, "analytics_student_0",
                 json.dumps({question_ids[0]: "Right", question_ids[1]: "False"}),
                 7, "", instructor_lms._now(), instructor_lms._now(), 2, "submitted", "graded"),
            )
        analytics = self.instructor.get(f"/api/lms/courses/{self.course['id']}/analytics").json()
        self.assertEqual(analytics["quiz_performance"][0]["average_percent"], 37.1)
        self.assertEqual(analytics["quiz_performance"][0]["grade_distribution"][0]["count"], 3)
        self.assertEqual(analytics["quiz_performance"][0]["grade_distribution"][4]["count"], 1)
        self.assertEqual(analytics["quiz_performance"][0]["grade_distribution"][5]["count"], 1)
        first_question = analytics["quiz_performance"][0]["question_performance"][0]
        self.assertEqual(first_question["responses"], 5)
        self.assertEqual(first_question["correct_percent"], 40)
        self.assertEqual(first_question["difficulty_indicator"], "higher_observed_difficulty")
        self.assertEqual(len(analytics["student_progress"]), 5)
        self.assertEqual(analytics["student_progress"][0]["trackable_items"], 1)
        self.assertEqual(analytics["student_progress"][0]["coverage_percent"], 100)
        self.assertFalse(analytics["lecture_engagement"]["available"])
        detail = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics/students/analytics_student_0",
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["items"][0]["grade"], 7)
        self.assertEqual(self.student.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).status_code, 403)
        self.assertEqual(self.student.get(
            f"/api/lms/courses/{self.course['id']}/analytics/students/analytics_student_0",
        ).status_code, 403)
        changed = self.instructor.put(f"/api/lms/quizzes/{quiz_id}", json={
            "title": "Changed", "status": "published", "questions": [{
                "prompt": "New question", "kind": "true_false", "answer": "True", "points": 1,
            }],
        })
        self.assertEqual(changed.status_code, 409)

    def test_video_checkpoint_questions_persist_responses_and_enforce_access(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        lecture_id = "checkpoint_lecture"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], None, "Checkpoint lecture", "checkpointmedia",
                None, "published", "{}", instructor_lms._now(), instructor_lms._now(),
            ))
        endpoint = f"/api/lms/lectures/{lecture_id}/checkpoints"
        payload = {
            "position_seconds": 4.5, "prompt": "Which option is correct?",
            "kind": "multiple_choice", "options": ["Correct", "Incorrect"], "answer": "Correct",
        }
        created = self.instructor.post(endpoint, json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        checkpoint_id = created.json()["id"]
        instructor_list = self.instructor.get(endpoint).json()["checkpoints"]
        self.assertEqual(instructor_list[0]["answer"], "Correct")
        student_list = self.student.get(endpoint).json()["checkpoints"]
        self.assertNotIn("answer", student_list[0])
        self.assertIsNone(student_list[0]["response"])
        self.assertEqual(self.instructor.post(
            f"{endpoint}/{checkpoint_id}/responses", json={"answer": "Correct"},
        ).status_code, 403)
        outsider = TestClient(self.instructor.app)
        self.addCleanup(outsider.close)
        outsider.post("/api/lms/auth/register", json={
            "name": "Unenrolled Learner", "email": "checkpoint-outsider@example.com",
            "password": "student-password-789", "role": "student",
        })
        self.assertEqual(outsider.get(endpoint).status_code, 404)
        self.assertEqual(outsider.post(
            f"{endpoint}/{checkpoint_id}/responses", json={"answer": "Correct"},
        ).status_code, 404)
        self.assertEqual(self.student.post(
            f"{endpoint}/{checkpoint_id}/responses", json={"answer": "Invalid"},
        ).status_code, 400)
        response = self.student.post(
            f"{endpoint}/{checkpoint_id}/responses", json={"answer": "Correct"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["is_correct"])
        self.assertEqual(self.student.post(
            f"{endpoint}/{checkpoint_id}/responses", json={"answer": "Incorrect"},
        ).status_code, 409)
        session_id = "66d1d149-2a7f-4e16-9ad3-c12bc42e6fa6"
        event_endpoint = f"/api/lms/lectures/{lecture_id}/video-events"
        self.assertEqual(self.student.post(event_endpoint, json={
            "session_id": session_id, "event_type": "checkpoint",
            "position_seconds": 4.5, "duration_seconds": 20, "checkpoint_id": "missing",
        }).status_code, 404)
        self.assertEqual(self.student.post(event_endpoint, json={
            "session_id": session_id, "event_type": "checkpoint",
            "position_seconds": 4.5, "duration_seconds": 20, "checkpoint_id": checkpoint_id,
        }).status_code, 200)
        self.assertEqual(self.instructor.put(
            f"/api/lms/checkpoints/{checkpoint_id}", json=payload,
        ).status_code, 409)
        self.assertEqual(self.instructor.delete(
            f"/api/lms/checkpoints/{checkpoint_id}",
        ).status_code, 409)
        analytics = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).json()
        checkpoint_metrics = analytics["checkpoint_performance"][0]
        self.assertEqual(checkpoint_metrics["responses"], 1)
        self.assertEqual(checkpoint_metrics["response_rate_percent"], 100)
        self.assertIsNone(checkpoint_metrics["correct_percent"])
        details = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics/students/{self.student_account['id']}",
        )
        self.assertEqual(details.status_code, 200, details.text)
        self.assertEqual(details.json()["checkpoint_responses"][0]["answer"], "Correct")
        invited = self.instructor.post("/api/lms/instructors", json={
            "name": "Other Instructor", "email": "other-instructor@example.com",
            "password": "instructor-password-456",
        })
        self.assertEqual(invited.status_code, 200, invited.text)
        other_instructor = TestClient(self.instructor.app)
        self.addCleanup(other_instructor.close)
        other_instructor.post("/api/lms/auth/login", json={
            "email": "other-instructor@example.com", "password": "instructor-password-456",
        })
        self.assertEqual(other_instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).status_code, 404)
        self.assertEqual(other_instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics/students/{self.student_account['id']}",
        ).status_code, 404)
        self.assertEqual(self.instructor.post(
            f"{endpoint}", json={**payload, "kind": "essay"},
        ).status_code, 400)

    def test_subjective_quiz_submission_manual_grading_and_grade_history(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        created = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Written assessment", "questions": [
                {"prompt": "Explain the concept.", "kind": "short_answer", "points": 4},
                {"prompt": "Discuss its significance.", "kind": "essay", "points": 6},
            ],
        })
        self.assertEqual(created.status_code, 200, created.text)
        quiz_id = created.json()["id"]
        edited = self.instructor.put(f"/api/lms/quizzes/{quiz_id}", json={
            "title": "Written assessment", "status": "published", "questions": [
                {"prompt": "Explain the concept.", "kind": "short_answer", "points": 4},
                {"prompt": "Discuss its significance.", "kind": "essay", "points": 6},
            ],
        })
        self.assertEqual(edited.status_code, 200, edited.text)

        quiz = self.student.get(f"/api/lms/students/quizzes/{quiz_id}").json()
        self.assertEqual([item["kind"] for item in quiz["questions"]], ["short_answer", "essay"])
        self.assertTrue(all(item["options"] == [] and "answer" not in item for item in quiz["questions"]))
        answers = {item["id"]: answer for item, answer in zip(
            quiz["questions"], ["A concise response.", "A longer considered response."]
        )}
        submitted = self.student.post(f"/api/lms/quizzes/{quiz_id}/submit", json={"answers": answers})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertIsNone(submitted.json()["grade"])
        self.assertEqual(submitted.json()["grading_status"], "pending")
        with instructor_lms._connect() as db:
            row = db.execute(
                "SELECT grade,grading_status,answers_json FROM submissions WHERE id=?",
                (submitted.json()["attempt_id"],),
            ).fetchone()
            self.assertIsNone(row["grade"])
            self.assertEqual(row["grading_status"], "pending")
            self.assertEqual(json.loads(row["answers_json"]), answers)

        student_grades = self.student.get(
            f"/api/lms/students/courses/{self.course['id']}/gradebook",
        ).json()
        self.assertIsNone(student_grades["grades"][0]["grade"])
        self.assertEqual(student_grades["grades"][0]["grading_status"], "pending")
        self.assertEqual(student_grades["totals"][0]["completed_quizzes"], 0)

        submission = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/submissions",
        ).json()["submissions"][0]
        self.assertEqual(submission["questions"][1]["kind"], "essay")
        self.assertEqual(submission["answers"], answers)
        self.assertEqual(submission["grade_history"], [])
        self.assertEqual(self.instructor.put(
            f"/api/lms/submissions/{submission['id']}/grade",
            json={"grade": 11, "feedback": "Over maximum"},
        ).status_code, 400)
        self.assertEqual(self.student.put(
            f"/api/lms/submissions/{submission['id']}/grade",
            json={"grade": 8, "feedback": "Not authorized"},
        ).status_code, 403)

        first_grade = self.instructor.put(
            f"/api/lms/submissions/{submission['id']}/grade",
            json={"grade": 7, "feedback": "Clear explanation."},
        )
        self.assertEqual(first_grade.status_code, 200, first_grade.text)
        revised_grade = self.instructor.put(
            f"/api/lms/submissions/{submission['id']}/grade",
            json={"grade": 8, "feedback": "Updated after review."},
        )
        self.assertEqual(revised_grade.status_code, 200, revised_grade.text)

        refreshed = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/submissions",
        ).json()["submissions"][0]
        self.assertEqual([entry["grade"] for entry in refreshed["grade_history"]], [7, 8])
        self.assertEqual([entry["source"] for entry in refreshed["grade_history"]], ["manual", "manual"])
        student_grades = self.student.get(
            f"/api/lms/students/courses/{self.course['id']}/gradebook",
        ).json()
        self.assertEqual(student_grades["grades"][0]["grade"], 8)
        self.assertEqual(student_grades["grades"][0]["feedback"], "Updated after review.")
        self.assertEqual(student_grades["totals"][0]["earned_points"], 8)

    def test_video_engagement_records_authorized_deduplicated_intervals(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        module = self.instructor.post(
            f"/api/lms/courses/{self.course['id']}/modules", json={"title": "Week one"},
        ).json()
        self.instructor.put(f"/api/lms/modules/{module['id']}", json={
            "title": module["title"], "status": "published",
        })
        lecture_id = "tracked_lecture"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], module["id"], "Tracked lecture", "trackedmedia", None,
                "published", "{}", instructor_lms._now(), instructor_lms._now(),
            ))
        endpoint = f"/api/lms/lectures/{lecture_id}/video-events"
        session_id = "150502df-ec34-4cb0-97ae-32943da50e11"

        self.assertEqual(self.instructor.post(endpoint, json={
            "session_id": session_id, "event_type": "play", "position_seconds": 0,
            "duration_seconds": 10,
        }).status_code, 403)
        anonymous = TestClient(self.instructor.app)
        self.addCleanup(anonymous.close)
        anonymous.cookies.clear()
        self.assertEqual(anonymous.post(endpoint, json={
            "session_id": session_id, "event_type": "play", "position_seconds": 0,
            "duration_seconds": 10,
        }).status_code, 401)
        outsider = TestClient(self.instructor.app)
        self.addCleanup(outsider.close)
        outsider.post("/api/lms/auth/register", json={
            "name": "Unenrolled Learner", "email": "outsider@example.com",
            "password": "student-password-789", "role": "student",
        })
        self.assertEqual(outsider.post(endpoint, json={
            "session_id": session_id, "event_type": "play", "position_seconds": 0,
            "duration_seconds": 10,
        }).status_code, 404)

        access = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "access", "position_seconds": 0,
            "duration_seconds": 10,
        })
        self.assertEqual(access.status_code, 200, access.text)
        access_metrics = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).json()["lecture_engagement"]
        self.assertTrue(access_metrics["available"])
        self.assertFalse(access_metrics["playback_available"])
        self.assertEqual(access_metrics["access_count"], 1)
        first = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "play", "position_seconds": 0,
            "duration_seconds": 10,
        })
        self.assertEqual(first.status_code, 200, first.text)
        time.sleep(1.1)
        progress = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "progress", "position_seconds": 3,
            "duration_seconds": 10,
        })
        self.assertEqual(progress.json()["unique_watched_seconds"], 3)
        self.assertEqual(self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "pause", "position_seconds": 3,
            "duration_seconds": 10,
        }).status_code, 200)
        time.sleep(1.1)
        paused_progress = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "progress", "position_seconds": 7,
            "duration_seconds": 10,
        }).json()
        self.assertEqual(paused_progress["unique_watched_seconds"], 3)
        self.assertEqual(self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "interrupted", "position_seconds": 7,
            "duration_seconds": 10,
        }).status_code, 200)
        self.assertEqual(self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "resume", "position_seconds": 3,
            "duration_seconds": 10,
        }).status_code, 200)
        time.sleep(1.1)
        second_progress = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "progress", "position_seconds": 7,
            "duration_seconds": 10,
        }).json()
        self.assertEqual(second_progress["unique_watched_seconds"], 7)
        self.assertEqual(self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "seek", "position_seconds": 9,
            "duration_seconds": 10,
        }).status_code, 200)
        time.sleep(1.1)
        completed = self.student.post(endpoint, json={
            "session_id": session_id, "event_type": "progress", "position_seconds": 10,
            "duration_seconds": 10,
        }).json()
        self.assertEqual(completed["unique_watched_seconds"], 8)
        self.assertEqual(completed["completion_status"], "in_progress")

        duplicate_session = "735897a7-c026-48bb-8e15-288aee468c36"
        self.student.post(endpoint, json={
            "session_id": duplicate_session, "event_type": "play", "position_seconds": 3,
            "duration_seconds": 10,
        })
        time.sleep(1.1)
        duplicate = self.student.post(endpoint, json={
            "session_id": duplicate_session, "event_type": "progress", "position_seconds": 7,
            "duration_seconds": 10,
        }).json()
        self.assertEqual(duplicate["unique_watched_seconds"], 8)
        completion_session = "2192875c-5464-49b2-9b96-bf1219a8b26a"
        self.student.post(endpoint, json={
            "session_id": completion_session, "event_type": "play", "position_seconds": 7,
            "duration_seconds": 10,
        })
        time.sleep(1.1)
        completion = self.student.post(endpoint, json={
            "session_id": completion_session, "event_type": "progress", "position_seconds": 9,
            "duration_seconds": 10,
        }).json()
        self.assertEqual(completion["unique_watched_seconds"], 10)
        self.assertEqual(completion["completion_status"], "completed")

        analytics_data = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).json()
        analytics = analytics_data["lecture_engagement"]
        self.assertTrue(analytics["available"])
        self.assertTrue(analytics["playback_available"])
        self.assertEqual(analytics["tracked_students"], 1)
        self.assertEqual(analytics["tracked_sessions"], 3)
        self.assertEqual(analytics["unique_watched_seconds"], 10)
        self.assertEqual(analytics["completion_count"], 1)
        self.assertIn("does not establish attention", analytics["interpretation"])
        self.assertEqual(analytics_data["module_progress"][0]["module_title"], "Week one")
        self.assertEqual(analytics_data["module_progress"][0]["completed_student_items"], 1)
        student_progress = analytics_data["student_progress"][0]
        self.assertEqual(student_progress["completed_items"], 1)
        self.assertEqual(student_progress["completion_percent"], 100)
        detail = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics/students/{self.student_account['id']}",
        ).json()
        self.assertEqual(detail["items"][0]["status"], "completed")
        self.assertEqual(detail["items"][0]["coverage_percent"], 100)
        with instructor_lms._connect() as db:
            event_types = {row["event_type"] for row in db.execute(
                "SELECT event_type FROM video_tracking_events WHERE lecture_id=?", (lecture_id,),
            ).fetchall()}
            self.assertTrue({
                "access", "play", "progress", "pause", "resume", "seek", "interrupted",
            } <= event_types)
            seek = db.execute(
                "SELECT from_seconds,to_seconds FROM video_tracking_events WHERE event_type='seek'",
            ).fetchone()
            self.assertIsNone(seek["from_seconds"])
            self.assertIsNone(seek["to_seconds"])
            self.assertGreater(db.execute(
                "SELECT SUM(active_seconds) FROM video_tracking_events WHERE lecture_id=?",
                (lecture_id,),
            ).fetchone()[0], 0)
        instructor_lms.init_db()
        persisted = self.instructor.get(
            f"/api/lms/courses/{self.course['id']}/analytics",
        ).json()["lecture_engagement"]
        self.assertEqual(persisted["unique_watched_seconds"], 10)

    def test_ai_question_suggestions_handle_provider_fallbacks_and_missing_configuration(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        lecture_id = "approved_ai_lecture"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], None, "Approved AI lecture", None, None, "published",
                json.dumps({"transcript": [{"start": 0, "text": "Water boils at 100 degrees Celsius."}]}),
                instructor_lms._now(), instructor_lms._now(),
            ))
        endpoint = f"/api/lms/lectures/{lecture_id}/quiz-suggestions"
        processor = types.SimpleNamespace(
            _refresh_clients=unittest.mock.Mock(),
            _provider_chain=unittest.mock.Mock(return_value=[]),
            _no_provider_message=unittest.mock.Mock(return_value="No AI providers are configured."),
        )
        with unittest.mock.patch.dict(sys.modules, {"main": types.SimpleNamespace(summary_processor=processor)}):
            unavailable = self.instructor.get(endpoint)
        self.assertEqual(unavailable.status_code, 503)
        self.assertEqual(unavailable.json()["detail"], "No AI providers are configured.")

        suggestion = {"questions": [{
            "prompt": "At what temperature does water boil?",
            "kind": "multiple_choice", "options": ["100 C", "0 C"],
            "answer": "100 C", "points": 2,
        }]}
        processor._provider_chain.return_value = [("gemini", "test-model"), ("openai", "test-model")]
        processor._complete = unittest.mock.Mock(side_effect=[
            TimeoutError("credential-value-must-not-be-logged"), json.dumps(suggestion),
        ])
        with unittest.mock.patch.dict(sys.modules, {"main": types.SimpleNamespace(summary_processor=processor)}):
            with self.assertLogs("instructor_lms", level="WARNING") as captured:
                generated = self.instructor.get(endpoint)
        self.assertEqual(generated.status_code, 200, generated.text)
        self.assertEqual(generated.json()["provider"], "openai")
        self.assertTrue(generated.json()["questions"][0]["requires_review"])
        self.assertNotIn("credential-value-must-not-be-logged", "\n".join(captured.output))
        rejected = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Needs confirmation", "questions": generated.json()["questions"],
        })
        self.assertEqual(rejected.status_code, 409)
        accepted = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Confirmed suggestions", "questions": generated.json()["questions"],
            "reviewed_suggestions": True,
        })
        self.assertEqual(accepted.status_code, 200, accepted.text)

        processor._complete.side_effect = [RuntimeError("rate limit"), RuntimeError("provider error")]
        with unittest.mock.patch.dict(sys.modules, {"main": types.SimpleNamespace(summary_processor=processor)}):
            failed = self.instructor.get(endpoint)
        self.assertEqual(failed.status_code, 502)
        self.assertNotIn("rate limit", failed.json()["detail"].lower())

    def test_quiz_attempt_save_resume_limits_deadlines_and_timeout(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        created = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Timed quiz", "time_limit_minutes": 10, "attempt_limit": 2,
            "questions": [{"prompt": "Select right", "kind": "multiple_choice",
                           "options": ["Right", "Wrong"], "answer": "Right", "points": 5}],
        })
        self.assertEqual(created.status_code, 200, created.text)
        quiz_id = created.json()["id"]
        self.assertEqual(self.instructor.put(f"/api/lms/quizzes/{quiz_id}", json={
            "status": "published",
        }).status_code, 200)

        first = self.student.post(f"/api/lms/quizzes/{quiz_id}/attempts")
        self.assertEqual(first.status_code, 200, first.text)
        attempt = first.json()["attempt"]
        question = first.json()["questions"][0]
        self.assertNotIn("answer", question)
        saved = self.student.put(f"/api/lms/quiz-attempts/{attempt['id']}", json={
            "answers": {question["id"]: "Right"},
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        resumed = self.student.post(f"/api/lms/quizzes/{quiz_id}/attempts").json()
        self.assertEqual(resumed["attempt"]["id"], attempt["id"])
        self.assertEqual(resumed["attempt"]["answers"][question["id"]], "Right")
        submitted = self.student.post(f"/api/lms/quiz-attempts/{attempt['id']}/submit", json={})
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["grade"], 5)
        self.assertTrue(submitted.json()["submitted_at"])
        with instructor_lms._connect() as db:
            persisted = db.execute("SELECT status,answers_json,grade FROM submissions WHERE id=?",
                                   (attempt["id"],)).fetchone()
        self.assertEqual(persisted["status"], "submitted")
        self.assertEqual(json.loads(persisted["answers_json"])[question["id"]], "Right")
        self.assertEqual(persisted["grade"], 5)
        self.assertEqual(self.student.post(
            f"/api/lms/quiz-attempts/{attempt['id']}/submit", json={},
        ).status_code, 409)

        second = self.student.post(f"/api/lms/quizzes/{quiz_id}/attempts")
        self.assertEqual(second.status_code, 200, second.text)
        second_attempt = second.json()["attempt"]
        self.assertEqual(second_attempt["attempt_number"], 2)
        self.assertEqual(self.student.post(
            f"/api/lms/quiz-attempts/{second_attempt['id']}/submit", json={"answers": {}},
        ).status_code, 400)
        self.assertEqual(self.student.post(f"/api/lms/quizzes/{quiz_id}/attempts").json()["attempt"]["id"],
                         second_attempt["id"])
        self.assertEqual(self.student.post(
            f"/api/lms/quiz-attempts/{second_attempt['id']}/submit",
            json={"answers": {question["id"]: "Wrong"}},
        ).json()["grade"], 0)
        self.assertEqual(self.student.post(f"/api/lms/quizzes/{quiz_id}/attempts").status_code, 409)
        totals = self.instructor.get(f"/api/lms/courses/{self.course['id']}/gradebook").json()["totals"][0]
        self.assertEqual(totals["earned_points"], 0)
        self.assertEqual(totals["available_points"], 5)

        empty_draft = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Removable draft", "questions": [
                {"prompt": "True?", "kind": "true_false", "answer": "True", "points": 1},
            ],
        }).json()
        self.assertEqual(self.instructor.delete(
            f"/api/lms/quizzes/{empty_draft['id']}",
        ).status_code, 200)
        self.assertEqual(self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Unreviewed AI", "reviewed_suggestions": False, "questions": [{
                "prompt": "Suggested?", "kind": "true_false", "options": ["True", "False"],
                "answer": "True", "points": 1, "requires_review": True,
            }],
        }).status_code, 409)

        timeout_quiz = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Short quiz", "time_limit_minutes": 1, "attempt_limit": 1,
            "questions": [{"prompt": "True?", "kind": "true_false", "answer": "True", "points": 1}],
        }).json()
        self.instructor.put(f"/api/lms/quizzes/{timeout_quiz['id']}", json={"status": "published"})
        timed = self.student.post(f"/api/lms/quizzes/{timeout_quiz['id']}/attempts").json()["attempt"]
        with instructor_lms._connect() as db:
            db.execute("UPDATE submissions SET started_at=? WHERE id=?", (
                (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat(), timed["id"],
            ))
        self.assertEqual(self.student.put(
            f"/api/lms/quiz-attempts/{timed['id']}", json={"answers": {}},
        ).status_code, 409)
        with instructor_lms._connect() as db:
            status = db.execute("SELECT status FROM submissions WHERE id=?", (timed["id"],)).fetchone()["status"]
        self.assertEqual(status, "expired")

        past_quiz = self.instructor.post(f"/api/lms/courses/{self.course['id']}/quizzes", json={
            "title": "Closed quiz", "due_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
            "questions": [{"prompt": "True?", "kind": "true_false", "answer": "True", "points": 1}],
        }).json()
        self.instructor.put(f"/api/lms/quizzes/{past_quiz['id']}", json={"status": "published"})
        self.assertEqual(self.student.post(f"/api/lms/quizzes/{past_quiz['id']}/attempts").status_code, 409)

    def test_ai_question_suggestions_require_approved_published_lecture(self):
        lecture_id = "lecture_unapproved"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], None, "Draft lecture", None, None, "draft",
                json.dumps({"transcript": [{"start": 0, "text": "Lecture content"}]}), None,
                instructor_lms._now(),
            ))
        self.assertEqual(self.instructor.get(
            f"/api/lms/lectures/{lecture_id}/quiz-suggestions",
        ).status_code, 409)
        with instructor_lms._connect() as db:
            db.execute("UPDATE lectures SET status='published' WHERE id=?", (lecture_id,))
        self.assertEqual(self.instructor.get(
            f"/api/lms/lectures/{lecture_id}/quiz-suggestions",
        ).status_code, 409)

    def test_lms_database_migrates_existing_single_submission_records(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(db_path)
            connection.executescript("""
                CREATE TABLE users (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE courses (
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '', status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE quizzes (
                    id TEXT PRIMARY KEY, course_id TEXT NOT NULL, title TEXT NOT NULL,
                    due_at TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE questions (
                    id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, prompt TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('multiple_choice','true_false')),
                    options_json TEXT NOT NULL, answer_key TEXT NOT NULL, points REAL NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE submissions (
                    id TEXT PRIMARY KEY, quiz_id TEXT NOT NULL, student_id TEXT NOT NULL,
                    answers_json TEXT NOT NULL, grade REAL, feedback TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT NOT NULL, UNIQUE(quiz_id, student_id)
                );
                INSERT INTO users VALUES('old_user','Old Instructor','old@example.com','hash','instructor','2026-01-01');
                INSERT INTO courses VALUES('old_course','old_user','Old course','','published','2026-01-01','2026-01-01');
                INSERT INTO quizzes VALUES('old_quiz','old_course','Old quiz',NULL,'published','2026-01-01');
                INSERT INTO questions VALUES('old_question','old_quiz','Legacy prompt','true_false','["True","False"]','True',1,0);
                INSERT INTO submissions VALUES('old_submission','old_quiz','old_user','{}',2,'Legacy feedback','2026-01-02');
            """)
            connection.commit()
            connection.close()

            with mock.patch.object(instructor_lms, "DB_PATH", db_path):
                instructor_lms.init_db()
                with instructor_lms._connect() as db:
                    course = db.execute("SELECT visibility,share_token FROM courses WHERE id='old_course'").fetchone()
                    quiz = db.execute("SELECT time_limit_minutes,attempt_limit FROM quizzes WHERE id='old_quiz'").fetchone()
                    submission = db.execute("SELECT * FROM submissions WHERE id='old_submission'").fetchone()
                    question = db.execute("SELECT * FROM questions WHERE id='old_question'").fetchone()
                    history = db.execute(
                        "SELECT source,grade FROM submission_grade_history WHERE submission_id='old_submission'",
                    ).fetchone()
                    self.assertEqual(course["visibility"], "private")
                    self.assertTrue(course["share_token"])
                    self.assertIsNone(quiz["time_limit_minutes"])
                    self.assertEqual(quiz["attempt_limit"], 1)
                    self.assertEqual(submission["status"], "submitted")
                    self.assertEqual(submission["attempt_number"], 1)
                    self.assertEqual(question["kind"], "true_false")
                    self.assertEqual((history["source"], history["grade"]), ("existing", 2))
                    db.execute("""INSERT INTO questions VALUES(
                        'new_subjective','old_quiz','Short answer','short_answer','[]','',3,1
                    )""")
                    db.execute("""INSERT INTO submissions(id,quiz_id,student_id,answers_json,feedback,
                                  submitted_at,started_at,attempt_number,status)
                                  VALUES('second','old_quiz','old_user','{}','', '2026-01-03',
                                  '2026-01-03',2,'submitted')""")
            instructor_lms.dispose_database(db_path)
    def test_generated_material_must_be_approved_before_publish(self):
        self.instructor.put(f"/api/lms/courses/{self.course['id']}", json={
            "title": "Foundations", "description": "", "status": "published",
        })
        video_id = "mediahash0002"
        instructor_lms.TRANSCRIPTS_DIR.mkdir()
        (instructor_lms.TRANSCRIPTS_DIR / f"{video_id}_whisper.json").write_text(
            json.dumps([{"start": 0, "text": "A generated transcript."}]), encoding="utf-8",
        )
        lecture_id = "lecture_ai"
        with instructor_lms._connect() as db:
            db.execute("INSERT INTO lectures VALUES(?,?,?,?,?,?,?,?,?,?)", (
                lecture_id, self.course["id"], None, "AI lecture", video_id, None, "draft",
                "{}", None, instructor_lms._now(),
            ))
        self.assertEqual(self.instructor.post(f"/api/lms/lectures/{lecture_id}/publish").status_code, 409)
        draft = self.instructor.get(f"/api/lms/lectures/{lecture_id}").json()
        self.assertEqual(draft["content"]["transcript"][0]["text"], "A generated transcript.")
        self.assertEqual(self.instructor.post(f"/api/lms/lectures/{lecture_id}/approve").status_code, 200)
        self.assertEqual(self.instructor.post(f"/api/lms/lectures/{lecture_id}/publish").status_code, 200)

    def student_request(self):
        from starlette.requests import Request

        token = self.student.cookies.get(instructor_lms.SESSION_COOKIE)
        return Request({
            "type": "http", "method": "GET", "path": "/", "headers": [
                (b"cookie", f"{instructor_lms.SESSION_COOKIE}={token}".encode()),
            ],
        })


if __name__ == "__main__":
    unittest.main()
