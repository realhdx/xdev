// Cordis host plug-in. ToolRunContext.agent is supplied by the Harness executor.
import { execFile } from 'node:child_process';
import path from 'node:path';

export const name = 'xdev-session-mailbox';
export const inject = ['tools', 'agents'];
const toolName = 'mcp__xdev__xdev_msg';
const fallback = 'mcp__xdev__xdev_msg_unbound';
const actions = ['whoami', 'send', 'forward', 'reply', 'read', 'receipt', 'inbox', 'list', 'wait', 'bind'];
const sessionPattern = /^session-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

export function caller(ctx, execution) {
  const agent = execution?.agent;
  const header = agent?.session?.header;
  if (!header || !sessionPattern.test(header.id) || ctx.agents.get(header.id) !== agent ||
      header.origin === 'subagent' || header.parentSession || (header.delegationDepth ?? 0) !== 0) {
    throw new Error('DSH could not bind this invocation to an original live chat.');
  }
  return header.id;
}

export function callMailbox(config, request, signal) {
  signal.throwIfAborted();
  const body = JSON.stringify(request);
  if (Buffer.byteLength(body) > 2 * 1024 * 1024) throw new Error('Mailbox request too large');
  return new Promise((resolve, reject) => {
    const child = execFile(config.python, ['-I', '-B', '-X', 'utf8', config.helper], {
      windowsHide: true, shell: false, encoding: 'utf8', signal,
      timeout: 625_000, maxBuffer: 32 * 1024 * 1024,
    }, (error, stdout) => {
      if (error) {
        reject(new Error('DSH mailbox operation was not confirmed. Check the original message ID before retrying.'));
        return;
      }
      try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
    child.stdin.on('error', () => {}); // Child termination is reported by execFile.
    child.stdin.end(body, 'utf8');
  });
}

export function definition(ctx, config, dispatch = callMailbox) {
  return {
    name: toolName,
    description: '自动识别本次调用的 DeepSeek 会话。whoami 查地址，inbox 收信，read 按 ID 确认，reply 回信，forward 转寄或摘引原信。--subject 设置或查询短题，回信和转寄默认沿用原题。send 可指定设备、客户端和会话，--also 可抄送。参数按 xdev msg 的 CLI 参数逐项传入。',
    parameters: { type: 'object', required: ['action', 'arguments'], additionalProperties: false,
      properties: { action: { type: 'string', enum: actions }, arguments: { type: 'array', items: {type: 'string'} } } },
    output: { schema: {type: 'object', additionalProperties: true},
      render: (_args, value) => [{type: 'text', text: JSON.stringify(value)}] },
    async execute(args, execution) {
      if (!args || !actions.includes(args.action) || !Array.isArray(args.arguments) ||
          !args.arguments.every(value => typeof value === 'string')) throw new Error('Invalid xdev message arguments');
      const session = caller(ctx, execution);
      const result = await dispatch(config, {session, action: args.action, arguments: args.arguments}, execution.signal);
      if (result.ok === false) throw new Error(JSON.stringify(result));
      return result;
    },
  };
}

export function apply(ctx, config) {
  if (!path.isAbsolute(config.python) || !path.isAbsolute(config.helper) ||
      path.basename(config.helper) !== 'dsh_host_tool.py') throw new Error('Invalid xdev host-tool configuration');
  let publishing = false, installed = false;
  const publish = () => {
    // The old stdio service can still own this name during hot reload. Wait for
    // the replacement service (and its fallback). No original tool is removed.
    if (installed || publishing || ctx.tools.get(toolName) || !ctx.tools.get(fallback)) return;
    publishing = true;
    try { ctx.tools.register(definition(ctx, config)); installed = true; }
    finally { publishing = false; }
  };
  ctx.on('tools/change', publish);
  publish();
}
