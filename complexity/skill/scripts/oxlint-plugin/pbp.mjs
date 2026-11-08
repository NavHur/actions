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
  if (parent?.type === "VariableDeclarator" && parent.id.type === "Identifier") return parent.id.name;
  if (parent?.type === "AssignmentExpression" && parent.left.type === "Identifier") return parent.left.name;
  if (["Property", "MethodDefinition", "PropertyDefinition"].includes(parent?.type) && !parent.computed) {
    return parent.key.name ?? String(parent.key.value ?? "<anonymous>");
  }
  return "<anonymous>";
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
    const value = node[key];
    if (Array.isArray(value)) {
      for (const item of value) if (item && typeof item.type === "string") yield item;
    } else if (value && typeof value.type === "string") {
      yield value;
    }
  }
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

function score(root) {
  let total = 0;
  const seenChains = new WeakSet();
  const frames = [];

  function walk(node, nesting) {
    if (!node) return;
    if (FUNCTIONS.has(node.type)) {
      frames.push({ forms: ownCallForms(node), counted: false });
      const depth = node === root ? nesting : nesting + 1;
      for (const child of children(node)) walk(child, depth);
      frames.pop();
      return;
    }
    if (node.type === "IfStatement") return visitIf(node, nesting, false);
    if (node.type === "ConditionalExpression") {
      total += 1 + nesting;
      walk(node.test, nesting);
      walk(node.consequent, nesting + 1);
      walk(node.alternate, nesting + 1);
      return;
    }
    if (LOOPS.has(node.type)) {
      total += 1 + nesting;
      for (const child of children(node)) walk(child, child === node.body ? nesting + 1 : nesting);
      return;
    }
    if (node.type === "SwitchStatement") {
      total += 1 + nesting;
      walk(node.discriminant, nesting);
      for (const branch of node.cases) for (const child of children(branch)) walk(child, nesting + 1);
      return;
    }
    if (node.type === "CatchClause") {
      total += 1 + nesting;
      walk(node.param, nesting);
      walk(node.body, nesting + 1);
      return;
    }
    if (node.type === "LogicalExpression" && node.operator !== "??" && !seenChains.has(node)) {
      total += runs(chainOperators(node, seenChains));
    }
    if ((node.type === "BreakStatement" || node.type === "ContinueStatement") && node.label) total += 1;
    if (node.type === "CallExpression" || node.type === "NewExpression") {
      const frame = frames[frames.length - 1];
      const form = callForm(node.callee);
      if (frame && !frame.counted && form && frame.forms.has(form)) {
        frame.counted = true;
        total += 1;
      }
    }
    for (const child of children(node)) walk(child, nesting);
  }

  function visitIf(node, nesting, elseIf) {
    total += elseIf ? 1 : 1 + nesting;
    walk(node.test, nesting);
    walk(node.consequent, nesting + 1);
    if (node.alternate?.type === "IfStatement") return visitIf(node.alternate, nesting, true);
    if (node.alternate) {
      total += 1;
      walk(node.alternate, nesting + 1);
    }
  }

  walk(root, 0);
  return total;
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
