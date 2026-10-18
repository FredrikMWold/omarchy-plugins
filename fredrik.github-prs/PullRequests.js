function query() {
  return "query($search: String!) {"
    + " search(query: $search, type: ISSUE, first: 30) { nodes {"
    + " ... on PullRequest { number title url author { login } isDraft"
    + " reviewDecision"
    + " latestOpinionatedReviews(first: 100) { nodes { state } pageInfo { hasNextPage } }"
    + " stackEntry { position }"
    + " stack { number size baseRefName"
    + " entries(first: 100) { nodes { position pullRequest { number title url } }"
    + " pageInfo { hasNextPage } } }"
    + " } } } }"
}

function parseResponse(raw) {
  var response = JSON.parse(raw)
  if (response.errors && response.errors.length > 0) {
    throw new Error(response.errors.map(function(error) { return error.message }).join("; "))
  }
  if (!response.data || !response.data.search || !Array.isArray(response.data.search.nodes)) {
    throw new Error("GitHub returned invalid PR data")
  }
  return response.data.search.nodes.filter(function(item) {
    return item && item.number !== undefined && item.isDraft !== true
  })
}

function reviewStatus(pullRequest) {
  var decision = pullRequest.reviewDecision
  if (decision === "CHANGES_REQUESTED") return "changes"
  if (decision === "APPROVED") return "approved"
  if (decision === "REVIEW_REQUIRED") return "required"

  var reviews = pullRequest.latestOpinionatedReviews
  if (!reviews || !reviews.nodes || typeof reviews.nodes.length !== "number") return "unknown"
  var approved = false
  for (var i = 0; i < reviews.nodes.length; i++) {
    var review = reviews.nodes[i]
    if (!review) continue
    if (review.state === "CHANGES_REQUESTED") return "changes"
    if (review.state === "APPROVED") approved = true
  }
  if (reviews.pageInfo && reviews.pageInfo.hasNextPage) return "unknown"
  return approved ? "approved" : "awaiting"
}

function reviewLabel(status) {
  if (status === "changes") return "Changes requested"
  if (status === "approved") return "Approved"
  if (status === "required") return "Review required"
  if (status === "awaiting") return "Awaiting review"
  return "Review status unknown"
}

function stackEntries(pullRequest) {
  var stack = pullRequest.stack
  if (!stack || !stack.entries || !stack.entries.nodes) return []
  var entries = []
  for (var i = 0; i < stack.entries.nodes.length; i++) {
    var entry = stack.entries.nodes[i]
    if (entry && entry.pullRequest) entries.push(entry)
  }
  return entries.sort(function(a, b) { return a.position - b.position })
}

function stackLabel(pullRequest) {
  var stack = pullRequest.stack
  if (!stack) return ""
  var position = pullRequest.stackEntry && pullRequest.stackEntry.position
  var label = "Stack " + (position ? position + "/" : "") + stack.size
  var entries = stackEntries(pullRequest)
  var previous = null
  var next = null
  for (var i = 0; i < entries.length; i++) {
    if (entries[i].position === position - 1) previous = entries[i].pullRequest
    if (entries[i].position === position + 1) next = entries[i].pullRequest
  }
  var chain = []
  if (position === 1) chain.push(stack.baseRefName)
  else if (previous) chain.push("#" + previous.number)
  chain.push("[#" + pullRequest.number + "]")
  if (next) chain.push("#" + next.number)
  return label + " \u00b7 " + chain.join(" \u2192 ")
}

function stackTooltip(pullRequest) {
  var stack = pullRequest.stack
  if (!stack) return ""
  var lines = ["Stack #" + stack.number + " \u00b7 base: " + stack.baseRefName]
  var entries = stackEntries(pullRequest)
  for (var i = 0; i < entries.length; i++) {
    var entry = entries[i]
    var current = entry.pullRequest.number === pullRequest.number
    lines.push((current ? "> " : "  ") + entry.position + ". #"
      + entry.pullRequest.number + " " + entry.pullRequest.title)
  }
  if (stack.entries && stack.entries.pageInfo && stack.entries.pageInfo.hasNextPage) {
    lines.push("More stack entries on GitHub")
  }
  return lines.join("\n")
}
