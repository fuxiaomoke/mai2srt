import { useCallback, useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'framer-motion';
import {
  Brain, CheckCircle2, Eye, KeyRound, Loader2, Plus, RefreshCw, Server, Trash2,
  Zap,
} from 'lucide-react';
import {
  Button, Card, ConfirmIconButton, Field, Select, useToast,
} from '../../components/ui';
import {
  llmActive, llmApplyDiscovery, llmDeleteProvider, llmDiscover, llmPresets,
  llmProviders, llmSaveProvider, llmSetActive, llmTest,
  warmActive, warmPresets, warmProviders, withBackendRetry,
  type ActiveLLM, type LlmPreset, type ModelMeta, type Provider,
} from '../../lib/api';
import { useI18n } from '../../lib/i18n';

/* ------------------------------------------------------------ model caps */

function ModelCaps({ m }: { m: ModelMeta }) {
  // "≈" marks a limit that came from the built-in name table rather than the
  // provider: the number also drives batching, so its trust level is worth
  // showing (no mark = reported by the API, nothing at all = unknown).
  const guessed = m.limits_source === 'builtin' ? '≈' : '';
  const k = (n: number) => (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
  return (
    <span className="flex items-center gap-1.5 text-ink-3">
      {m.reasoning && <Brain size={12} strokeWidth={2} aria-label="reasoning" />}
      {m.vision && <Eye size={12} strokeWidth={2} aria-label="vision" />}
      {m.context_window && (
        <span className="font-mono text-[10px]">
          {guessed}{k(m.context_window)}
        </span>
      )}
      {m.max_output && (
        <span className="font-mono text-[10px]" title="max output tokens">
          out {guessed}{k(m.max_output)}
        </span>
      )}
    </span>
  );
}

/* --------------------------------------------------------- settings > llm */

export function LlmSettings() {
  const { t, tf } = useI18n();
  // seeded from the startup warm cache: a hit means the first frame is
  // already complete -- no "no providers yet" card flashing before the
  // real cards, no selects filling in. A cold cache stays empty AND
  // unpainted (the ready gate below), so the empty value is never shown.
  const [providers, setProviders] = useState<Provider[]>(() => warmProviders() ?? []);
  const [active, setActive] = useState<ActiveLLM | null>(warmActive);
  const [presets, setPresets] = useState<Record<string, LlmPreset>>(() => warmPresets() ?? {});
  // cold cache only (backend was down at startup): gate until loaded
  const [ready, setReady] = useState(() => warmProviders() !== null);
  const [adding, setAdding] = useState(false);
  // provider chosen in the active-model row (falls back to the active one)
  const [selProviderId, setSelProviderId] = useState<string | null>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      // retry-wrapped: a cold/slow backend (packaged first launch) must
      // not strand the page with an empty preset/provider list
      const [ps, pr, ac] = await withBackendRetry(() =>
        Promise.all([
          llmPresets(), llmProviders(), llmActive(),
        ]));
      setPresets(ps);
      setProviders(pr);
      setActive(ac);
    } catch {
      /* backend offline */
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // provider shown in the active-model row: explicit choice wins, else the
  // provider of the currently active model; stale picks fall back safely
  const selProvider =
    providers.find((p) => p.id === (selProviderId ?? active?.provider)) ??
    providers.find((p) => p.id === active?.provider) ??
    null;

  async function activate(pid: string, mid: string) {
    try {
      const a = await llmSetActive({ provider: pid, model: mid, effort: active?.effort ?? 'low' });
      setActive(a);
      setSelProviderId(pid);
      toast('ok', tf('s.toast.activeSet', { model: mid }));
    } catch (e) {
      toast('error', String(e).slice(0, 140));
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[min(1280px,94%)] flex-col gap-5 px-8 py-8">
      {ready && (
        <>
      {/* active model: provider dropdown + that provider's models */}
      <Card className="flex flex-wrap items-center gap-4 p-5">
        <Server size={16} strokeWidth={2} className="text-primary" />
        <span className="text-[13px] font-semibold text-ink-1">{t('s.activeModel')}</span>
        <Select
          className="w-44"
          value={selProvider?.id ?? ''}
          placeholder={t('s.select.provider')}
          onChange={(pid) => {
            const p = providers.find((x) => x.id === pid);
            setSelProviderId(pid);
            if (!p) return;
            // keep the current model if this provider also has it, else
            // activate the provider's first model right away
            const mid = p.models.some((m) => m.id === active?.model)
              ? active!.model
              : p.models[0]?.id;
            if (mid) activate(pid, mid);
          }}
          options={providers.map((p) => ({ value: p.id, label: p.name }))}
        />
        <Select
          className="min-w-0 flex-1"
          value={active && active.provider === selProvider?.id ? active.model : ''}
          placeholder={selProvider?.models.length ? t('s.select.model') : t('s.select.noModels')}
          onChange={(mid) => selProvider && activate(selProvider.id, mid)}
          options={(selProvider?.models ?? []).map((m) => ({ value: m.id, label: m.id }))}
        />
        <Select
          className="w-36"
          value={active?.effort ?? 'low'}
          onChange={async (e) => {
            if (!active) return;
            const a = await llmSetActive({ ...active, effort: e });
            setActive(a);
          }}
          options={[
            { value: 'off', label: `${t('s.effort')}: off` },
            { value: 'low', label: `${t('s.effort')}: low` },
            { value: 'medium', label: `${t('s.effort')}: medium` },
            { value: 'high', label: `${t('s.effort')}: high` },
            { value: 'max', label: `${t('s.effort')}: max` },
          ]}
        />
      </Card>

      {/* providers */}
      <div className="flex items-center justify-between">
        <span className="text-[13px] font-semibold text-ink-1">{t('s.providers')}</span>
        <Button variant="ghost" lift onClick={() => setAdding((a) => !a)}>
          <Plus size={14} strokeWidth={2} /> {t('s.addProvider')}
        </Button>
      </div>

      <AnimatePresence>
        {adding && (
          /* height-only: this wrapper contains a GLASS card, and an
             opacity animation isolates the compositor layer -- during
             the fade backdrop-filter samples nothing, so the card sits
             flat gray until opacity lands and the glass tint (the dark
             glow behind it) pops in late. Height + overflow-hidden
             reveals it cleanly with the filter live the whole time. */
          <motion.div initial={{ height: 0 }} animate={{ height: 'auto' }} exit={{ height: 0 }} className="overflow-hidden">
            <AddProviderCard
              presets={presets}
              onSaved={() => { setAdding(false); refresh(); }}
              onCancel={() => setAdding(false)}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {providers.length === 0 && !adding && (
        <Card className="p-6 text-center text-[13px] text-ink-3">
          {t('s.noProviders')}
        </Card>
      )}

      {providers.map((p) => (
        <ProviderCard key={p.id} provider={p} active={active} onChanged={refresh} />
      ))}
        </>
      )}
    </div>
  );
}

/* ------------------------------------------------------------ add provider */

function AddProviderCard({
  presets, onSaved, onCancel,
}: {
  presets: Record<string, LlmPreset>;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [presetId, setPresetId] = useState('deepseek');
  const [name, setName] = useState('DeepSeek 官方');
  const [baseUrl, setBaseUrl] = useState('https://api.deepseek.com');
  const [apiKey, setApiKey] = useState('');
  const [protocol, setProtocol] = useState<'openai' | 'anthropic' | 'gemini'>('openai');
  const { t, tf } = useI18n();
  const toast = useToast();

  function pickPreset(id: string) {
    setPresetId(id);
    const p = presets[id];
    if (p) {
      setName(p.name);
      setBaseUrl(p.base_url);
      setProtocol((p.protocol as typeof protocol) ?? 'openai');
    }
  }

  /** switch API format: while the URL still holds one of this preset's
   *  default endpoints it follows the format switch (OpenRouter openai
   *  https://openrouter.ai/api/v1 <-> anthropic https://openrouter.ai/api;
   *  DeepSeek likewise). A hand-typed URL is never clobbered. */
  function pickProtocol(p: 'openai' | 'anthropic' | 'gemini') {
    const preset = presets[presetId];
    if (preset) {
      const defaults: Record<string, string> = {
        [preset.protocol]: preset.base_url,
        ...preset.alt_base_urls,
      };
      if (Object.values(defaults).includes(baseUrl)) {
        const next = defaults[p];
        if (next && next !== baseUrl) setBaseUrl(next);
      }
    }
    setProtocol(p);
  }

  async function save() {
    const preset = presets[presetId];
    const authStyle =
      protocol === 'anthropic' ? 'x-api-key'
      : protocol === 'gemini' ? 'query-key'
      : (preset?.auth_style ?? 'bearer');
    try {
      await llmSaveProvider({
        id: presetId === 'custom' ? undefined : presetId,
        name,
        protocol,
        base_url: baseUrl,
        api_key: apiKey || '***',
        auth_style: authStyle,
        models: [],
      });
      toast('ok', tf('s.toast.providerSaved', { name }));
      onSaved();
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    }
  }

  return (
    <Card className="flex flex-col gap-4 p-5">
      <div className="flex flex-wrap gap-2">
        {Object.entries(presets).map(([id, p]) => (
          <button
            key={id}
            onClick={() => pickPreset(id)}
            className={`rounded-full border px-3.5 py-1.5 text-[12px] transition-colors duration-(--dur-in) ${
              presetId === id
                ? 'border-primary bg-primary-dim text-primary'
                : 'border-line-1 text-ink-2 hover:border-line-2'
            }`}
          >
            {p.label ?? p.name}
          </button>
        ))}
      </div>
      {/* API protocol (three formats; presets preselect, custom is free) */}
      <div className="flex items-center gap-3">
        <span className="text-[12px] font-medium text-ink-2">API</span>
        {(['openai', 'anthropic', 'gemini'] as const).map((p) => (
          <button
            key={p}
            onClick={() => pickProtocol(p)}
            className={`rounded-full border px-3.5 py-1.5 text-[12px] font-mono transition-colors duration-(--dur-in) ${
              protocol === p
                ? 'border-primary bg-primary-dim text-primary'
                : 'border-line-1 text-ink-2 hover:border-line-2'
            }`}
          >
            {p}
          </button>
        ))}
      </div>
      <div className="grid grid-cols-2 gap-4">
        <Field label={t('s.f.name')} value={name} onChange={(e) => setName(e.target.value)} />
        <Field label={t('s.f.baseUrl')} value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
        <Field
          label={t('s.f.apiKey')}
          type="password"
          value={apiKey}
          placeholder="sk-..."
          onChange={(e) => setApiKey(e.target.value)}
        />
      </div>
      <div className="flex gap-2">
        <Button onClick={save} disabled={!name || !baseUrl}>
          <KeyRound size={14} strokeWidth={2} /> {t('s.saveProvider')}
        </Button>
        <Button variant="ghost" onClick={onCancel}>{t('s.cancel')}</Button>
      </div>
    </Card>
  );
}

/* ---------------------------------------------------------- provider card */

function ProviderCard({
  provider, active, onChanged,
}: {
  provider: Provider;
  active: ActiveLLM | null;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<'test' | 'discover' | null>(null);
  // per-row model test, holds the model id being pinged (null = idle)
  const [testing, setTesting] = useState<string | null>(null);
  const { t, tf } = useI18n();
  const toast = useToast();
  const isActive = active?.provider === provider.id;

  // The card-level button tests the model you actually transcribe with when
  // this provider is the active one -- but only while it still exists in the
  // list (a discovery refresh can drop it). Otherwise it falls back to the
  // first listed model, which is exactly what the backend does when no model
  // is named, so the tooltip never lies about the target.
  const activeModel = isActive && active?.model
    && provider.models.some((m) => m.id === active.model)
    ? active.model
    : null;
  const testTarget = activeModel ?? provider.models[0]?.id ?? '';

  /** model given  -> that row's model only (row button)
   *  model omitted -> the card's target: active model, else first listed */
  async function test(model?: string) {
    const target = model ?? testTarget;
    if (!target) {
      toast('warn', t('s.toast.testNoModel'));
      return;
    }
    if (model) setTesting(model); else setBusy('test');
    try {
      const r = await llmTest(provider.id, target);
      toast('ok', tf('s.toast.testOk', { name: provider.name, model: r.model }));
    } catch (e) {
      toast('error', `${provider.name}: ${String(e).slice(0, 140)}`);
    } finally {
      if (model) setTesting(null); else setBusy(null);
    }
  }

  async function discover() {
    setBusy('discover');
    try {
      const { diff, discovered } = await llmDiscover(provider.id);
      if (discovered.length === 0) {
        toast('warn', t('s.toast.discoverNone'));
      } else {
        await llmApplyDiscovery(provider.id, discovered);
        toast(
          'ok',
          tf('s.toast.discoverApplied', {
            added: diff.added.length,
            removed: diff.removed.length,
            changed: diff.changed.length,
          }),
        );
      }
      onChanged();
    } catch (e) {
      toast('error', `${provider.name}: ${String(e).slice(0, 140)}`);
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    try {
      await llmDeleteProvider(provider.id);
      toast('ok', tf('s.toast.providerRemoved', { name: provider.name }));
      onChanged();
    } catch (e) {
      toast('error', String(e).slice(0, 140));
    }
  }

  return (
    <Card className="flex flex-col p-5">
      <div className="flex items-center gap-3">
        <button onClick={() => setOpen((o) => !o)} className="flex min-w-0 flex-1 items-center gap-3 text-left">
          <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-(--radius-s) ${
            isActive ? 'bg-primary-dim text-primary' : 'bg-sunken text-ink-2'
          }`}>
            <Server size={16} strokeWidth={2} />
          </span>
          <div className="min-w-0">
            <p className="truncate text-[13px] font-semibold text-ink-1">
              {provider.name}
              {isActive && <span className="ml-2 text-[10.5px] font-medium text-primary">{t('s.activeBadge')}</span>}
            </p>
            <p className="truncate font-mono text-[10.5px] text-ink-3">
              {provider.protocol} · {provider.base_url} · {tf('s.modelsCount', { n: provider.models.length })}
            </p>
          </div>
        </button>
        <div className="flex items-center gap-1.5">
          <Button
            variant="ghost"
            lift
            busy={busy === 'test'}
            onClick={() => test()}
            title={testTarget ? tf('s.hint.test', { model: testTarget }) : t('s.hint.testNoModel')}
          >
            {t('s.test')}
          </Button>
          <Button variant="ghost" lift busy={busy === 'discover'} onClick={discover} title={t('s.hint.discover')}>
            <RefreshCw size={13} strokeWidth={2} /> {t('s.discover')}
          </Button>
          {/* destructive, so it takes the same two-step arming as every other
              delete in the app: first click arms, second removes */}
          <ConfirmIconButton
            icon={<Trash2 size={14} strokeWidth={2} />}
            onConfirm={remove}
            title={t('s.hint.removeProvider')}
            confirmTitle={t('s.storage.deleteConfirm')}
            hint={t('s.deleteHint')}
          />
        </div>
      </div>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="mt-4 border-t border-line-1 pt-3">
              {provider.models.length === 0 && (
                <p className="py-3 text-[12.5px] text-ink-3">
                  {t('s.noModels')}
                </p>
              )}
              {provider.models.map((m) => (
                <div
                  key={m.id}
                  className={`flex items-center gap-3 rounded-(--radius-s) px-3 py-2 ${
                    active?.model === m.id && isActive ? 'bg-primary-dim' : 'hover:bg-hover'
                  }`}
                >
                  <span className="flex-1 truncate font-mono text-[12px] text-ink-1">{m.id}</span>
                  <ModelCaps m={m} />
                  <button
                    onClick={() => test(m.id)}
                    disabled={testing !== null && testing !== m.id}
                    className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-primary disabled:opacity-40 disabled:pointer-events-none"
                    title={tf('s.hint.testModel', { model: m.id })}
                  >
                    {testing === m.id
                      ? <Loader2 size={14} strokeWidth={2} className="animate-spin" />
                      : <Zap size={14} strokeWidth={2} />}
                  </button>
                  <button
                    onClick={async () => {
                      try {
                        await llmSetActive({ provider: provider.id, model: m.id, effort: 'low' });
                        toast('ok', tf('s.toast.activeSet', { model: m.id }));
                        onChanged();
                      } catch (e) {
                        toast('error', String(e).slice(0, 140));
                      }
                    }}
                    className="grid h-7 w-7 shrink-0 place-items-center rounded-(--radius-s) text-ink-3 transition-colors hover:bg-hover hover:text-primary"
                    title={t('s.hint.setActive')}
                  >
                    <CheckCircle2 size={14} strokeWidth={2} />
                  </button>
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </Card>
  );
}
