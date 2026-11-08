// Oxlint JS plugin `pbp`: Cognitive Complexity per the SonarSource whitepaper
// (G. Ann Campbell, v1.7), scored the same way as fleet's pbp-complexity (crates/pbp-complexity/src/scorer.rs).
// measure_complexity.py loads it for repositories whose oxlint config has no
// cognitive-complexity rule of its own.

const FUNCTIONS = new Set(["FunctionDeclaration", "FunctionExpression", "ArrowFunctionExpression"]);
const LOOPS = new Set(["ForStatement", "ForInStatement", "ForOfStatement", "WhileStatement", "DoWhileStatement"]);
const SKIPPED_KEYS = new Set(["parent", "loc", "range", "start", "end", "type", "leadingComments", "trailingComments"]);

function functionName(node) {
  if (node.id?.name) return node.id.name;
  const parent = node.parent;
  switch (parent?.type) {
    case "VariableDeclarator":
      return identifierName(parent.id);
    case "AssignmentExpression":
      return identifierName(parent.left);
    case "Property":
    case "MethodDefinition":
    case "PropertyDefinition":
      return propertyName(parent);
  }
  return "<anonymous>";
}

function identifierName(node) {
  return node.type === "Identifier" ? node.name : "<anonymous>";
}

function propertyName(node) {
  if (node.computed) return "<anonymous>";
  return node.key.name ?? String(node.key.value ?? "<anonymous>");
}

// Call forms that count as recursion for this function. A bare name inside a
// method or object property refers to an outer binding, so those recurse only
// through `this.`.
function ownCallForms(node) {
  const name = functionName(node);
  if (name === "<anonymous>") return new Set();
  if (["MethodDefinition", "PropertyDefinition", "Property"].includes(node.parent?.type)) {
    return new Set([`this.${name}`]);
  }
  return new Set([name]);
}

function callForm(callee) {
  if (callee.type === "Identifier") return callee.name;
  if (callee.type === "MemberExpression" && !callee.computed && callee.object.type === "ThisExpression") {
    return `this.${callee.property.name}`;
  }
  return null;
}

function* children(node) {
  for (const key of Object.keys(node)) {
    if (SKIPPED_KEYS.has(key)) continue;
    yield* childNodes(node[key]);
  }
}

function childNodes(value) {
  const values = Array.isArray(value) ? value : [value];
  return values.filter((item) => item && typeof item.type === "string");
}

// Operators of a logical chain in source order; `??` and non-logical nodes end it.
function chainOperators(node, seen, operators = []) {
  if (node?.type !== "LogicalExpression" || node.operator === "??") return operators;
  seen.add(node);
  chainOperators(node.left, seen, operators);
  operators.push(node.operator);
  chainOperators(node.right, seen, operators);
  return operators;
}

function runs(operators) {
  return operators.filter((operator, index) => operator !== operators[index - 1]).length;
}

const VISITORS = {
  IfStatement: "visitIf",
  ConditionalExpression: "visitConditional",
  SwitchStatement: "visitSwitch",
  CatchClause: "visitCatch",
  LogicalExpression: "visitLogical",
  BreakStatement: "visitJump",
  ContinueStatement: "visitJump",
  CallExpression: "visitCall",
  NewExpression: "visitCall",
};

class CognitiveScore {
  constructor(root) {
    this.root = root;
    this.total = 0;
    this.seenChains = new WeakSet();
    this.frames = [];
  }

  walk(node, nesting) {
    if (!node) return;
    if (FUNCTIONS.has(node.type)) return this.visitFunction(node, nesting);
    if (LOOPS.has(node.type)) return this.visitLoop(node, nesting);
    const visitor = VISITORS[node.type];
    if (visitor) return this[visitor](node, nesting);
    this.walkChildren(node, nesting);
  }

  walkChildren(node, nesting) {
    for (const child of children(node)) this.walk(child, nesting);
  }

  visitFunction(node, nesting) {
    this.frames.push({ forms: ownCallForms(node), counted: false });
    this.walkChildren(node, node === this.root ? nesting : nesting + 1);
    this.frames.pop();
  }

  visitConditional(node, nesting) {
    this.total += 1 + nesting;
    this.walk(node.test, nesting);
    this.walk(node.consequent, nesting + 1);
    this.walk(node.alternate, nesting + 1);
  }

  visitLoop(node, nesting) {
    this.total += 1 + nesting;
    for (const child of children(node)) this.walk(child, child === node.body ? nesting + 1 : nesting);
  }

  visitSwitch(node, nesting) {
    this.total += 1 + nesting;
    this.walk(node.discriminant, nesting);
    for (const branch of node.cases) this.walkChildren(branch, nesting + 1);
  }

  visitCatch(node, nesting) {
    this.total += 1 + nesting;
    this.walk(node.param, nesting);
    this.walk(node.body, nesting + 1);
  }

  visitLogical(node, nesting) {
    if (node.operator !== "??" && !this.seenChains.has(node)) {
      this.total += runs(chainOperators(node, this.seenChains));
    }
    this.walkChildren(node, nesting);
  }

  visitJump(node, nesting) {
    if (node.label) this.total += 1;
    this.walkChildren(node, nesting);
  }

  visitCall(node, nesting) {
    const frame = this.frames[this.frames.length - 1];
    const form = callForm(node.callee);
    if (frame && !frame.counted && form && frame.forms.has(form)) {
      frame.counted = true;
      this.total += 1;
    }
    this.walkChildren(node, nesting);
  }

  visitIf(node, nesting, elseIf = false) {
    this.total += elseIf ? 1 : 1 + nesting;
    this.walk(node.test, nesting);
    this.walk(node.consequent, nesting + 1);
    if (node.alternate?.type === "IfStatement") return this.visitIf(node.alternate, nesting, true);
    if (node.alternate) {
      this.total += 1;
      this.walk(node.alternate, nesting + 1);
    }
  }
}

function score(root) {
  const result = new CognitiveScore(root);
  result.walk(root, 0);
  return result.total;
}

function enclosedByFunction(node) {
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (FUNCTIONS.has(parent.type)) return true;
  }
  return false;
}

const cognitiveComplexity = {
  meta: {
    type: "suggestion",
    docs: { description: "Cognitive Complexity of each top-level function, nested functions included." },
    schema: [{ type: "object", properties: { max: { type: "integer", minimum: 0 } }, additionalProperties: false }],
  },
  create(context) {
    const max = context.options[0]?.max ?? 15;
    function check(node) {
      if (enclosedByFunction(node)) return;
      const value = score(node);
      if (value > max) {
        context.report({
          node,
          message: `Function \`${functionName(node)}\` has cognitive complexity ${value}, above the limit of ${max}.`,
        });
      }
    }
    return { FunctionDeclaration: check, FunctionExpression: check, ArrowFunctionExpression: check };
  },
};

export default { meta: { name: "pbp" }, rules: { "cognitive-complexity": cognitiveComplexity } };
