import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from pydantic import BaseModel
from dotenv import load_dotenv

from agents import run_planner, run_developer, run_qa, run_reviewer

load_dotenv()

app = FastAPI(title="AI DevTeam Studio")
templates = Jinja2Templates(directory="templates")

class RequirementRequest(BaseModel):
    requirement: str

@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    # Naye Starlette/FastAPI version mein request pehle aata hai
    return templates.TemplateResponse(request=request, name="index.html")

@app.post("/api/run-workflow")
async def execute_workflow(data: RequirementRequest):
    if not os.getenv("GROQ_API_KEY"):
        raise HTTPException(status_code=500, detail="GROQ_API_KEY missing in .env file")
    
    req = data.requirement.strip()
    if not req:
        raise HTTPException(status_code=400, detail="Requirement cannot be empty")

    try:
        os.makedirs("output", exist_ok=True)

        # 1. Project Planner
        plan = run_planner(req)
        with open("output/implementation_plan.md", "w", encoding="utf-8") as f:
            f.write(plan)

        # 2. Python Developer
        code = run_developer(plan)
        with open("output/app.py", "w", encoding="utf-8") as f:
            f.write(code)

        # 3. QA Engineer
        test_code = run_qa(code)
        with open("output/test_app.py", "w", encoding="utf-8") as f:
            f.write(test_code)

        # 4. Code Reviewer
        review = run_reviewer(code, test_code)
        with open("output/development_report.md", "w", encoding="utf-8") as f:
            f.write(review)

        return {
            "status": "success",
            "plan": plan,
            "code": code,
            "tests": test_code,
            "review": review
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))