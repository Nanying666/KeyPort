'use client';

import { useCallback, useEffect, useState } from 'react';
import { api, ApiError, type ApiKeyView } from '@/lib/api';
import { PageHeader } from '@/components/shell';
import {
  AlertDialog,
  Badge,
  Button,
  Card,
  EmptyState,
  Field,
  Meter,
  Modal,
  Toggle,
} from '@/components/ui';
import { useToast } from '@/components/use-toast';
import { formatNumber, formatRelative } from '@/lib/utils';

interface KeyForm {
  name: string;
  realm: string;
  model_allow: string;
  quota_credits: string;
  quota_tokens: string;
  ip_allow: string;
  ip_block: string;
  note: string;
}

const EMPTY: KeyForm = {
  name: '',
  realm: '',
  model_allow: '',
  quota_credits: '',
  quota_tokens: '',
  ip_allow: '',
  ip_block: '',
  note: '',
};

function splitLines(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * 积分配额是否在该域失效。
 *
 * z.ai 的 usage 不含 credit 字段（额度按 token 计），后端 extract_credits 恒返回 0，
 * 于是 used_credits 永远为 0、credits_exhausted() 永远 False —— 设了也不生效。
 * 统一用 Token 配额可规避这一差异。
 */
function creditQuotaInert(realm: string): boolean {
  return realm === 'zai';
}

export default function KeysPage() {
  const [keys, setKeys] = useState<ApiKeyView[]>([]);
  const [realms, setRealms] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<KeyForm>(EMPTY);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [createdKey, setCreatedKey] = useState('');
  const [pending, setPending] = useState<{ kind: 'reset' | 'delete'; id: number } | null>(null);
  const { show, node } = useToast();

  const load = useCallback(async () => {
    try {
      const [keyRes, accRes] = await Promise.all([
        api.get<{ keys: ApiKeyView[] }>('/api/keys'),
        api.get<{ realms: string[] }>('/api/accounts'),
      ]);
      setKeys(keyRes.keys);
      setRealms(accRes.realms);
    } catch (err) {
      show(err instanceof ApiError ? err.message : '加载失败', 'err');
    }
  }, [show]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    try {
      const res = await api.post<{ key: string }>('/api/keys', {
        name: form.name || 'default',
        realm: form.realm,
        model_allow: splitLines(form.model_allow),
        quota_credits: Number(form.quota_credits) || 0,
        quota_tokens: Number(form.quota_tokens) || 0,
        ip_allow: splitLines(form.ip_allow),
        ip_block: splitLines(form.ip_block),
        note: form.note,
      });
      setCreatedKey(res.key);
      setForm(EMPTY);
      setOpen(false);
      await load();
      show('密钥已创建');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '创建失败', 'err');
    }
  }

  /** 打开编辑：把当前行数据回填到表单（明文不可取回，故不涉及密钥本身）。 */
  function openEdit(key: ApiKeyView) {
    setEditingId(key.id);
    setForm({
      name: key.name,
      realm: key.realm,
      model_allow: key.model_allow.join('\n'),
      quota_credits: key.quota_credits ? String(key.quota_credits) : '',
      quota_tokens: key.quota_tokens ? String(key.quota_tokens) : '',
      ip_allow: key.ip_allow.join('\n'),
      ip_block: key.ip_block.join('\n'),
      note: key.note,
    });
    setOpen(true);
  }

  function closeForm() {
    setOpen(false);
    setEditingId(null);
    setForm(EMPTY);
  }

  async function save() {
    if (editingId === null) return;
    try {
      await api.patch(`/api/keys/${editingId}`, {
        name: form.name || 'default',
        realm: form.realm,
        model_allow: splitLines(form.model_allow),
        quota_credits: Number(form.quota_credits) || 0,
        quota_tokens: Number(form.quota_tokens) || 0,
        ip_allow: splitLines(form.ip_allow),
        ip_block: splitLines(form.ip_block),
        note: form.note,
      });
      closeForm();
      await load();
      show('密钥已更新');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '保存失败', 'err');
    }
  }

  async function resetUsage(id: number) {
    setPending(null);
    try {
      await api.patch(`/api/keys/${id}`, { reset_usage: true });
      await load();
      show('用量已清零');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '清零失败', 'err');
    }
  }

  async function toggleEnabled(key: ApiKeyView, enabled: boolean) {
    try {
      await api.patch(`/api/keys/${key.id}`, { enabled });
      await load();
    } catch (err) {
      show(err instanceof ApiError ? err.message : '更新失败', 'err');
    }
  }

  async function remove(id: number) {
    setPending(null);
    try {
      await api.del(`/api/keys/${id}`);
      await load();
      show('已删除');
    } catch (err) {
      show(err instanceof ApiError ? err.message : '删除失败', 'err');
    }
  }

  return (
    <>
      <PageHeader
        title="密钥管理"
        description="多密钥分发：模型白名单、独立配额、IP 限制；明文仅创建时展示一次"
        actions={
          <Button
            variant="primary"
            onClick={() => {
              setEditingId(null);
              setForm(EMPTY);
              setOpen(true);
            }}
          >
            创建密钥
          </Button>
        }
      />

      {createdKey ? (
        <Card className="mb-4">
          <p className="mb-2 text-xs font-medium text-[hsl(var(--caution))]">
            新密钥已创建 —— 明文仅此一次展示，请立即复制保存
          </p>
          <div className="flex flex-wrap items-center gap-2">
            <code className="flex-1 break-all rounded bg-[hsl(var(--well))] px-3 py-2 font-mono text-xs">
              {createdKey}
            </code>
            <Button
              variant="primary"
              size="sm"
              onClick={() => {
                void navigator.clipboard.writeText(createdKey);
                show('已复制');
              }}
            >
              复制
            </Button>
            <Button variant="outline" size="sm" onClick={() => setCreatedKey('')}>
              我已保存
            </Button>
          </div>
        </Card>
      ) : null}

      <Card className="p-0">
        <div className="scroll-thin overflow-x-auto">
          <table className="data has-actions">
            <thead>
              <tr>
                <th>名称</th>
                <th>前缀</th>
                <th>域</th>
                <th>模型白名单</th>
                <th>额度</th>
                <th>Token</th>
                <th>IP 规则</th>
                <th>最后使用</th>
                <th>启用</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k.id}>
                  <td className="font-medium text-[hsl(var(--ink))]">{k.name}</td>
                  <td className="mono text-[11px]">{k.key_prefix}…</td>
                  <td>
                    {k.realm ? <Badge tone="neutral">{k.realm}</Badge> : '不限'}
                  </td>
                  <td className="text-[11px]">{k.model_allow.join(', ') || '不限'}</td>
                  <td>
                    <div className="flex flex-col gap-1">
                      <span className="mono text-[11px]">
                        {formatNumber(k.used_credits)} /{' '}
                        {k.quota_credits ? formatNumber(k.quota_credits) : '∞'}
                      </span>
                      {k.quota_credits ? (
                        <Meter value={k.used_credits} max={k.quota_credits} className="w-16" />
                      ) : null}
                    </div>
                  </td>
                  <td>
                    <div className="flex flex-col gap-1">
                      <span className="mono text-[11px] text-[hsl(var(--ink))]">
                        {formatNumber(k.used_tokens)} /{' '}
                        {k.quota_tokens ? formatNumber(k.quota_tokens) : '∞'}
                      </span>
                      {k.quota_tokens ? (
                        <Meter value={k.used_tokens} max={k.quota_tokens} className="w-16" />
                      ) : null}
                    </div>
                  </td>
                  <td className="text-[11px]">
                    {k.ip_allow.length ? `白 ${k.ip_allow.length}` : ''}
                    {k.ip_block.length ? ` 黑 ${k.ip_block.length}` : ''}
                    {!k.ip_allow.length && !k.ip_block.length ? '不限' : ''}
                  </td>
                  <td className="text-[11px]">{formatRelative(k.last_used_at)}</td>
                  <td>
                    <Toggle
                      checked={k.enabled}
                      onChange={(v) => void toggleEnabled(k, v)}
                      label="启用"
                    />
                  </td>
                  <td>
                    <div className="flex flex-wrap gap-1">
                      <Button size="sm" variant="outline" onClick={() => openEdit(k)}>
                        编辑
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setPending({ kind: 'reset', id: k.id })}
                      >
                        清零
                      </Button>
                      <Button
                        size="sm"
                        variant="danger"
                        onClick={() => setPending({ kind: 'delete', id: k.id })}
                      >
                        删除
                      </Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {keys.length === 0 ? <EmptyState message="暂无密钥，点击右上角创建" /> : null}
      </Card>

      <Modal
        open={open}
        title={editingId === null ? '创建密钥' : '编辑密钥'}
        onClose={closeForm}
        footer={
          <>
            <Button variant="outline" onClick={closeForm}>
              取消
            </Button>
            <Button
              variant="primary"
              onClick={() => void (editingId === null ? create() : save())}
            >
              {editingId === null ? '创建' : '保存'}
            </Button>
          </>
        }
      >
        <div className="flex flex-col gap-3">
          <Field label="名称">
            <input
              className="input"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
          </Field>
          <Field label="限定账号域" hint="留空 = 不限域">
            <select
              className="input"
              value={form.realm}
              onChange={(e) => setForm({ ...form, realm: e.target.value })}
            >
              <option value="">不限</option>
              {realms.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </Field>
          <Field label="模型白名单" hint="每行或逗号分隔；留空 = 全部模型">
            <textarea
              className="input h-20"
              value={form.model_allow}
              onChange={(e) => setForm({ ...form, model_allow: e.target.value })}
            />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Token 配额" hint="0 = 不限；两域通用，推荐">
              <input
                className="input"
                type="number"
                value={form.quota_tokens}
                onChange={(e) => setForm({ ...form, quota_tokens: e.target.value })}
              />
            </Field>
            <Field
              label="积分配额"
              hint={creditQuotaInert(form.realm) ? 'z.ai 域下不生效' : '0 = 不限'}
            >
              <input
                className="input"
                type="number"
                value={form.quota_credits}
                onChange={(e) => setForm({ ...form, quota_credits: e.target.value })}
                disabled={creditQuotaInert(form.realm)}
              />
            </Field>
          </div>
          {creditQuotaInert(form.realm) ? (
            <p className="text-[11px] text-[hsl(var(--caution))]">
              z.ai 的额度按 Token 计，usage 不带积分字段，积分配额不会生效。请用左侧
              「Token 配额」控制用量。
            </p>
          ) : null}
          <Field label="IP 白名单" hint="CIDR，每行一个；非空则仅这些 IP 可用">
            <textarea
              className="input h-16"
              value={form.ip_allow}
              onChange={(e) => setForm({ ...form, ip_allow: e.target.value })}
              placeholder="10.0.0.0/8"
            />
          </Field>
          <Field label="IP 黑名单" hint="CIDR，每行一个；优先于白名单">
            <textarea
              className="input h-16"
              value={form.ip_block}
              onChange={(e) => setForm({ ...form, ip_block: e.target.value })}
            />
          </Field>
          <Field label="备注">
            <input
              className="input"
              value={form.note}
              onChange={(e) => setForm({ ...form, note: e.target.value })}
            />
          </Field>
        </div>
      </Modal>
      <AlertDialog
        open={pending !== null}
        title={pending?.kind === 'reset' ? '清零用量' : '删除密钥'}
        message={
          pending?.kind === 'reset'
            ? '将把该密钥的已用额度与 Token 计数清零，配额上限保持不变。'
            : '删除后使用该密钥的客户端将立即失效，此操作不可撤销。'
        }
        confirmLabel={pending?.kind === 'reset' ? '清零' : '删除'}
        onConfirm={() =>
          pending?.kind === 'reset' ? void resetUsage(pending.id) : void remove(pending!.id)
        }
        onCancel={() => setPending(null)}
      />
      {node}
    </>
  );
}
