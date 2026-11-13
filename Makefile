# The gate's steps, run by fleet's pre-commit skill. CI runs the same ones.
.PHONY: format lint typecheck test

format:
	ruff format .

lint:
	ruff check .

typecheck:
	ty check

test:
	python3 -m unittest discover -s file-size -p "test_*.py"
	python3 -m unittest discover -s ci-cache -p "test_*.py"
	python3 -m unittest discover -s complexity -p "test_*.py"
	python3 -m unittest discover -s checks -p "test_*.py"
