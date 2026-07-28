import http from "node:http";

const server = http.createServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString("utf8");

  if (request.method !== "POST" || request.url !== "/v1/chat/completions") {
    response.writeHead(404, { "content-type": "application/json" });
    response.end(JSON.stringify({ error: "not found" }));
    return;
  }

  const payload = JSON.parse(body);
  const prompt = payload.messages
    .map((message) =>
      typeof message.content === "string"
        ? message.content
        : JSON.stringify(message.content),
    )
    .join("\n");
  const content = prompt.includes("KELPIE_STDIN_MARKER")
    ? "KELPIE_STDIN_CONFIRMED"
    : "KELPIE_STDIN_MISSING";

  response.writeHead(200, {
    "content-type": "text/event-stream",
    "cache-control": "no-cache",
    connection: "keep-alive"
  });
  response.write(
    `data: ${JSON.stringify({
      id: "kelpie-mock",
      object: "chat.completion.chunk",
      created: 0,
      model: payload.model,
      choices: [
        {
          index: 0,
          delta: { role: "assistant", content },
          finish_reason: null
        }
      ]
    })}\n\n`,
  );
  response.write(
    `data: ${JSON.stringify({
      id: "kelpie-mock",
      object: "chat.completion.chunk",
      created: 0,
      model: payload.model,
      choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
      usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 }
    })}\n\n`,
  );
  response.end("data: [DONE]\n\n");
});

server.listen(18080, "0.0.0.0", () => {
  process.stdout.write("kelpie mock Ollama listening on 18080\n");
});
