@echo off
cd backend
uvicorn main:app --reload --port 8001
