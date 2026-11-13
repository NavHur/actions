# The gate's steps, run by fleet's pre-commit skill. CI runs the same ones.
.PHONY: format lint typecheck test

format:
	ruff format .

lint:
	ruff check .

typecheck:
	ty check

# complexity runs the action in a scratch repository; this repository's own diff is mostly copies of fleet code.
# checks runs file-size and complexity in one run: the second still runs when the first fails, and either failure fails it.
test:
	python3 -m unittest discover -s file-size -p "test_*.py"
	python3 -m unittest discover -s ci-cache -p "test_*.py"
	python3 -m unittest discover -s complexity -p "test_*.py"
	python3 -m unittest discover -s checks -p "test_*.py"
