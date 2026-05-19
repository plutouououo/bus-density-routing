# Makefile
dev-frontend:
	cd frontend && npm run dev

dev-backend:
	cd backend && source venv/Scripts/activate && uvicorn main:app --reload --port 8000

dev:
	make dev-backend & make dev-frontend