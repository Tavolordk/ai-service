export interface IaChatRequest {
  profile: { profileId: string; [key: string]: unknown };
  question: string;
  selectedNode?: Record<string, unknown> | null;
  graph?: Record<string, unknown> | null;
  history?: Array<{ role: 'user' | 'assistant' | 'system'; content: string }>;
  thinking?: boolean;
  mode?: 'quick' | 'auto' | 'deep';
  caseId?: string;
}

export async function streamIa(
  req: IaChatRequest,
  onDelta: (text: string) => void,
  onReplace: (text: string) => void,
  onDone: (text: string) => void,
) {
  const response = await fetch('http://localhost:3651/api/v1/intelligence/chat/stream', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...req, thinking: req.thinking ?? false }),
  });

  if (!response.ok || !response.body) throw new Error(`IA HTTP ${response.status}`);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  const consume = (block: string) => {
    const dataLine = block.split('\n').find(line => line.startsWith('data: '));
    if (!dataLine) return;
    const data = JSON.parse(dataLine.slice(6));
    if (data.type === 'delta') onDelta(data.text ?? '');
    if (data.type === 'replace') onReplace(data.text ?? '');
    if (data.type === 'done') onDone(data.text ?? '');
    if (data.type === 'error') throw new Error(data.message);
    // No se renderiza ningún reasoning/thinking privado aunque una API antigua lo envíe.
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const blocks = buffer.split('\n\n');
    buffer = blocks.pop() ?? '';
    for (const block of blocks) consume(block);
  }

  if (buffer.trim()) consume(buffer);
}
