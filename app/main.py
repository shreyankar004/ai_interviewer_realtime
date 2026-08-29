import os
import re
import json
import time
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from typing import Optional, Literal
from pydantic import BaseModel
from fastapi.responses import Response, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST



load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"



MAX_QUESTIONS = 6



app = FastAPI(title="AI Interviewer Service")




app.mount(
    "/static",
    StaticFiles(directory="static"),
    name="static"
)

templates = Jinja2Templates(directory="templates")




REQUEST_COUNT = Counter(
    "requests_total",
    "Total number of requests",
    ["endpoint", "method"]
)

REQUEST_LATENCY = Histogram(
    "request_latency_seconds",
    "Request latency",
    ["endpoint", "method"]
)

ERROR_COUNT = Counter(
    "errors_total",
    "Total number of errors",
    ["endpoint", "method"]
)



SESSION = {}




class AnswerPayload(BaseModel):
    answer: str
    input_method: Literal["voice", "text"] = "text"



@app.middleware("http")
async def add_metrics(request: Request, call_next):

    start_time = time.time()

    endpoint = request.url.path
    method = request.method

    try:

        response = await call_next(request)

        latency = time.time() - start_time

        REQUEST_COUNT.labels(
            endpoint=endpoint,
            method=method
        ).inc()

        REQUEST_LATENCY.labels(
            endpoint=endpoint,
            method=method
        ).observe(latency)

        return response

    except Exception:

        ERROR_COUNT.labels(
            endpoint=endpoint,
            method=method
        ).inc()

        raise


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/healthz")
async def healthz():

    return {
        "status": "ok"
    }


# ============================================================
# HOME PAGE
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request
        }
    )




@app.post("/submit_role")
async def submit_role(
    request: Request,
    interview_role: str = Form(...),
    candidate_years_of_experience: str = Form(...),
    job_important_skills: Optional[str] = Form(None),
    job_level: str = Form(...),
):

    # Save job information
    SESSION["interview_role"] = interview_role
    SESSION["candidate_years_of_experience"] = candidate_years_of_experience
    SESSION["job_important_skills"] = job_important_skills
    SESSION["job_level"] = job_level

    # Create interviewer system prompt
    system_prompt = (
        f"You are an AI interviewer for a {interview_role} position. "
        f"Interview a {job_level} candidate with "
        f"{candidate_years_of_experience} years of experience. "
        f"Focus on skills: {job_important_skills}. "
        "Ask exactly one question at a time, adjusting difficulty "
        "based on their previous answers. "
        "Keep each question to 1-3 sentences. "
        "Do not restate these instructions to the candidate."
    )

    # Initialize conversation
    SESSION["history"] = [
        {
            "role": "system",
            "content": system_prompt
        }
    ]

    SESSION["question_count"] = 0

    # Structured log of question/answer/input_method pairs, kept separate
    # from the raw Groq chat "history" above. This is what powers the
    # post-interview analysis and must not be cleared until evaluation
    # is complete.
    SESSION["qa_log"] = []
    SESSION["pending_question"] = None
    SESSION["evaluation"] = None

    return RedirectResponse(
        url="/interview",
        status_code=303
    )




@app.get("/interview", response_class=HTMLResponse)
async def interview(request: Request):

    return templates.TemplateResponse(
        "interview.html",
        {
            "request": request
        }
    )




def call_groq(history: list, max_tokens: int = 200) -> str:

    """
    Sends the conversation history to Groq's
    OpenAI-compatible Chat Completions API.
    """

    

    if not GROQ_API_KEY:

        raise RuntimeError(
            "GROQ_API_KEY is not set. "
            "Check your .env file."
        )


    
    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


    
    payload = {
        "model": GROQ_MODEL,
        "messages": history,
        "temperature": 0.7,
        "max_completion_tokens": max_tokens,
    }


    

    try:

        response = requests.post(
            GROQ_URL,
            headers=headers,
            json=payload,
            timeout=30,
        )

    except requests.exceptions.Timeout:

        raise RuntimeError(
            "Groq API request timed out. "
            "Please try again."
        )

    except requests.exceptions.ConnectionError as e:

        raise RuntimeError(
            f"Could not connect to Groq API: {str(e)}"
        )

    except requests.exceptions.RequestException as e:

        raise RuntimeError(
            f"Groq request failed: {str(e)}"
        )


    
    if not response.ok:

        try:

            error_data = response.json()

            error_message = error_data.get(
                "error",
                error_data
            )

        except ValueError:

            error_message = response.text


        raise RuntimeError(
            f"Groq API error "
            f"(HTTP {response.status_code}): "
            f"{error_message}"
        )


    
    try:

        data = response.json()

    except ValueError:

        raise RuntimeError(
            "Groq returned an invalid JSON response."
        )


    
    try:

        content = data["choices"][0]["message"]["content"]

    except (KeyError, IndexError, TypeError):

        raise RuntimeError(
            f"Unexpected Groq response: {data}"
        )


    if not content:

        raise RuntimeError(
            "Groq returned an empty response."
        )


    return content.strip()



@app.get("/start_interview")
async def start_interview():

    history = SESSION.get("history")

    if not history:

        return JSONResponse(
            {
                "error": (
                    "No active interview. "
                    "Submit role details first."
                )
            },
            status_code=400
        )


    # Add first-question instruction
    history.append(
        {
            "role": "user",
            "content": "Begin the interview with your first question."
        }
    )


    # Call Groq
    try:

        question = call_groq(history)

    except RuntimeError as e:

        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=500
        )


    # Save assistant response
    history.append(
        {
            "role": "assistant",
            "content": question
        }
    )


    SESSION["question_count"] = 1
    SESSION["pending_question"] = question


    return JSONResponse(
        {
            "question": question,
            "done": False
        }
    )




@app.post("/respond")
async def respond(payload: AnswerPayload):

    history = SESSION.get("history")

    if not history:

        return JSONResponse(
            {
                "error": (
                    "No active interview. "
                    "Submit role details first."
                )
            },
            status_code=400
        )


    

    answer = payload.answer.strip()

    if not answer:

        return JSONResponse(
            {
                "error": "Answer cannot be empty."
            },
            status_code=400
        )


    
    history.append(
        {
            "role": "user",
            "content": answer
        }
    )


    # Record this question/answer pair (with how it was answered) for the
    # post-interview analysis. This log is independent of the raw Groq
    # chat history and is never cleared until /analyze_interview runs.
    qa_log = SESSION.setdefault("qa_log", [])
    qa_log.append(
        {
            "question": SESSION.get("pending_question") or "",
            "answer": answer,
            "input_method": payload.input_method,
        }
    )


    question_count = SESSION.get(
        "question_count",
        0
    )


    

    if question_count >= MAX_QUESTIONS:

        history.append(
            {
                "role": "user",
                "content": (
                    "That was the last answer. "
                    "Thank the candidate and briefly "
                    "close out the interview in 1-2 sentences."
                )
            }
        )


        try:

            closing = call_groq(history)

        except RuntimeError as e:

            return JSONResponse(
                {
                    "error": str(e)
                },
                status_code=500
            )


        history.append(
            {
                "role": "assistant",
                "content": closing
            }
        )

        SESSION["pending_question"] = None

        return JSONResponse(
            {
                "question": closing,
                "done": True
            }
        )


    

    try:

        question = call_groq(history)

    except RuntimeError as e:

        return JSONResponse(
            {
                "error": str(e)
            },
            status_code=500
        )


    # Save assistant question
    history.append(
        {
            "role": "assistant",
            "content": question
        }
    )


    # Increment question counter
    SESSION["question_count"] = question_count + 1
    SESSION["pending_question"] = question


    return JSONResponse(
        {
            "question": question,
            "done": False
        }
    )


# ============================================================
# POST-INTERVIEW ANALYSIS
# ============================================================

def _strip_code_fences(text: str) -> str:
    """
    Groq sometimes wraps JSON responses in ```json ... ``` fences.
    Safely remove them (and any stray leading/trailing prose) before
    attempting to parse.
    """

    text = text.strip()

    # Remove ```json ... ``` or ``` ... ``` fences
    fence_match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence_match:
        return fence_match.group(1).strip()

    # Fall back to grabbing the outermost {...} block, in case the model
    # added commentary before/after the JSON object.
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return brace_match.group(0).strip()

    return text


def _clamp_score(value, default=0) -> int:
    try:
        score = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def _build_evaluation_prompt(qa_log: list) -> list:
    """
    Builds a fresh, standalone chat-completion prompt (separate from the
    interview conversation) asking Groq to evaluate the finished
    interview and return structured JSON.
    """

    interview_role = SESSION.get("interview_role", "Unknown role")
    job_level = SESSION.get("job_level", "Unknown level")
    experience = SESSION.get("candidate_years_of_experience", "Unknown")
    skills = SESSION.get("job_important_skills") or "Not specified"

    transcript_lines = []
    for i, qa in enumerate(qa_log, start=1):
        transcript_lines.append(
            f"Q{i} ({qa.get('input_method', 'text')} answer): "
            f"{qa.get('question', '')}\n"
            f"A{i}: {qa.get('answer', '')}"
        )
    transcript = "\n\n".join(transcript_lines)

    system_prompt = (
        "You are an expert interview evaluator. You will be given the "
        "full transcript of a completed job interview and must score the "
        "candidate's performance strictly based on the content of their "
        "answers.\n\n"
        f"Position being interviewed for: {interview_role}\n"
        f"Candidate seniority level: {job_level}\n"
        f"Candidate years of experience: {experience}\n"
        f"Key skills relevant to this role: {skills}\n\n"
        "Tailor your evaluation criteria to this specific role. A "
        "Python Developer, an HR Manager, and a Machine Learning "
        "Engineer should be judged on different technical expectations. "
        "Weigh answers appropriately for the candidate's stated "
        "seniority level.\n\n"
        "Some answers were given by voice (transcribed by the browser) "
        "and some were typed. Evaluate every answer purely on its "
        "content, quality, correctness, and relevance. Do NOT comment on "
        "or penalize accent, speech patterns, personality, mental state, "
        "gender, age, or any attribute unrelated to the substance of the "
        "answer. Minor transcription artifacts in voice answers should "
        "not be held against the candidate.\n\n"
        "Respond with ONLY a single valid JSON object, no markdown code "
        "fences, no commentary before or after, matching exactly this "
        "shape:\n"
        "{\n"
        '  "overall_score": <integer 0-100>,\n'
        '  "category_scores": {\n'
        '    "answer_quality": <integer 0-100>,\n'
        '    "technical_knowledge": <integer 0-100>,\n'
        '    "communication": <integer 0-100>,\n'
        '    "problem_solving": <integer 0-100>,\n'
        '    "relevance": <integer 0-100>\n'
        "  },\n"
        '  "overall_assessment": "<2-4 sentence professional summary>",\n'
        '  "strengths": ["<specific strength>", "... (3-5 total)"],\n'
        '  "areas_for_improvement": ["<specific improvement area>", "... (3-5 total)"],\n'
        '  "question_feedback": [\n'
        "    {\n"
        '      "question_number": <integer>,\n'
        '      "question": "<the question text>",\n'
        '      "answer": "<the candidate answer text>",\n'
        '      "score": <integer 0-100>,\n'
        '      "feedback": "<1-3 sentence specific feedback>"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "overall_score must be a reasonable aggregate of the category "
        "scores (roughly their average), not an arbitrary number. Base "
        "every score, strength, weakness, and piece of feedback on what "
        "the candidate actually said in the transcript below."
    )

    user_prompt = (
        "Here is the full interview transcript to evaluate:\n\n"
        f"{transcript}\n\n"
        "Return the JSON evaluation now."
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _fallback_evaluation(qa_log: list, error_message: str) -> dict:
    """
    Used only if the AI response cannot be parsed at all, so the user
    still gets a usable (if generic) report instead of a hard failure.
    """

    return {
        "overall_score": 0,
        "category_scores": {
            "answer_quality": 0,
            "technical_knowledge": 0,
            "communication": 0,
            "problem_solving": 0,
            "relevance": 0,
        },
        "overall_assessment": (
            "We couldn't generate a detailed report automatically. "
            f"({error_message}) You can try running the evaluation again."
        ),
        "strengths": [],
        "areas_for_improvement": [],
        "question_feedback": [
            {
                "question_number": i + 1,
                "question": qa.get("question", ""),
                "answer": qa.get("answer", ""),
                "score": 0,
                "feedback": "Not evaluated due to a report generation error.",
            }
            for i, qa in enumerate(qa_log)
        ],
        "error": True,
    }


def _normalize_evaluation(raw: dict, qa_log: list) -> dict:
    """
    Fills in any missing pieces and clamps scores so the frontend always
    receives a predictable, safe-to-render shape.
    """

    category_raw = raw.get("category_scores") or {}
    category_scores = {
        "answer_quality": _clamp_score(category_raw.get("answer_quality")),
        "technical_knowledge": _clamp_score(category_raw.get("technical_knowledge")),
        "communication": _clamp_score(category_raw.get("communication")),
        "problem_solving": _clamp_score(category_raw.get("problem_solving")),
        "relevance": _clamp_score(category_raw.get("relevance")),
    }

    overall_score = raw.get("overall_score")
    if overall_score is None:
        values = list(category_scores.values())
        overall_score = sum(values) / len(values) if values else 0
    overall_score = _clamp_score(overall_score)

    strengths = raw.get("strengths")
    if not isinstance(strengths, list):
        strengths = []
    strengths = [str(s) for s in strengths][:5]

    improvements = raw.get("areas_for_improvement")
    if not isinstance(improvements, list):
        improvements = []
    improvements = [str(s) for s in improvements][:5]

    question_feedback_raw = raw.get("question_feedback")
    if not isinstance(question_feedback_raw, list):
        question_feedback_raw = []

    question_feedback = []
    for i, qa in enumerate(qa_log, start=1):
        match = next(
            (
                item for item in question_feedback_raw
                if isinstance(item, dict) and item.get("question_number") == i
            ),
            question_feedback_raw[i - 1] if i - 1 < len(question_feedback_raw) else {}
        )
        if not isinstance(match, dict):
            match = {}

        question_feedback.append(
            {
                "question_number": i,
                "question": qa.get("question", ""),
                "answer": qa.get("answer", ""),
                "input_method": qa.get("input_method", "text"),
                "score": _clamp_score(match.get("score")),
                "feedback": str(match.get("feedback", "")).strip() or "No feedback provided.",
            }
        )

    return {
        "overall_score": overall_score,
        "category_scores": category_scores,
        "overall_assessment": str(raw.get("overall_assessment", "")).strip()
        or "No summary was generated.",
        "strengths": strengths,
        "areas_for_improvement": improvements,
        "question_feedback": question_feedback,
    }


@app.post("/analyze_interview")
async def analyze_interview():

    qa_log = SESSION.get("qa_log")

    if not qa_log:
        return JSONResponse(
            {
                "error": (
                    "No interview answers found to analyze. "
                    "Please complete at least one question first."
                )
            },
            status_code=400
        )

    eval_prompt = _build_evaluation_prompt(qa_log)

    try:
        # Evaluation JSON (scores + per-question feedback) needs a much
        # larger token budget than the short interview questions.
        raw_text = call_groq(eval_prompt, max_tokens=2000)
    except RuntimeError as e:
        # Keep qa_log intact so the user can retry evaluation without
        # losing their interview data.
        return JSONResponse(
            {
                "error": (
                    "We couldn't generate the interview report. "
                    f"{str(e)}"
                )
            },
            status_code=500
        )

    cleaned = _strip_code_fences(raw_text)

    try:
        parsed = json.loads(cleaned)
        if not isinstance(parsed, dict):
            raise ValueError("Evaluation response was not a JSON object.")
        evaluation = _normalize_evaluation(parsed, qa_log)
    except (ValueError, json.JSONDecodeError):
        evaluation = _fallback_evaluation(
            qa_log,
            "The evaluator returned a response we couldn't parse.",
        )

    SESSION["evaluation"] = evaluation

    return JSONResponse(evaluation)


@app.get("/metrics")
async def metrics():

    return Response(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )