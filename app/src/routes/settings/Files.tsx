import { useCallback, useEffect, useState } from 'react';
import {
  Compass, Eraser, LogIn, Plus, ShieldCheck, UserRound,
} from 'lucide-react';
import {
  Button, Card, Select, StatusDot, Toggle, useToast,
} from '../../components/ui';
import {
  addAccount, createJob, deleteAccount, getSession, getSystem, setBrowser,
  setDeleteMode, subscribeJob, switchAccount, warmSession, warmSystem,
  withBackendRetry,
  type SessionInfo, type SystemInfo,
} from '../../lib/api';
import { useI18n } from '../../lib/i18n';
import { StorageCard } from './StorageCard';

/* -------------------------------------------------- settings > files & accounts */

export function FilesSettings() {
  const { t, tf } = useI18n();
  // seed from the startup warm cache: when it hits, the very first render
  // is already complete (and refresh() below just revalidates silently)
  const [sys, setSys] = useState<SystemInfo | null>(warmSystem);
  const [session, setSession] = useState<SessionInfo | null>(warmSession);
  // cold cache only (backend was down at startup): stay unpainted until
  // the mount fetches settle, so no pre-data state is ever shown
  const [ready, setReady] = useState(() => warmSystem() !== null && warmSession() !== null);
  const [newAccount, setNewAccount] = useState('');
  const [loginBusy, setLoginBusy] = useState(false);
  const [consentBusy, setConsentBusy] = useState(false);
  /** set by a sign-in that found the account behind the 451 biometric
   *  gate: promotes the consent button to primary until it is accepted */
  const [consentNeeded, setConsentNeeded] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [confirmForgetFor, setConfirmForgetFor] = useState<string | null>(null);
  const toast = useToast();

  const refresh = useCallback(async () => {
    try {
      // retry-wrapped: a cold/slow backend (packaged first launch) must
      // not strand the page with an empty environment card
      const [s, se] = await withBackendRetry(() =>
        Promise.all([getSystem(), getSession()]));
      setSys(s);
      setSession(se);
    } catch {
      /* backend offline */
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  async function signIn() {
    setLoginBusy(true);
    try {
      const job = await createJob({ kind: 'login' });
      subscribeJob(job.id, {
        onLog: () => {},
        onDone: (d) => {
          // a fresh account usually owes the biometric notice: the login
          // already checked it server-side -- point here BEFORE the first
          // transcription discovers the 451 the hard way. Sticky: it must
          // not vanish on a timer, the user closes it (or accepting does)
          if (d.consent_needed) {
            setConsentNeeded(true);
            toast('warn', t('s.toast.consentNeeded'), {
              sticky: true,
              tag: 'consent-needed',
            });
          } else {
            setConsentNeeded(false);
            toast.dismiss('consent-needed');
            toast('ok', t('s.toast.signinOk'));
          }
          refresh();
          setLoginBusy(false);
        },
        onError: (m) => { toast('error', m.slice(0, 160)); setLoginBusy(false); },
        onCancelled: () => setLoginBusy(false),
      });
    } catch (e) {
      toast('error', String(e).slice(0, 160));
      setLoginBusy(false);
    }
  }

  /** issue #1: accept the one-time playground biometric notice (BIPA) that
   *  otherwise hard-fails every audio upload with HTTP 451. Runs through the
   *  job machinery like signIn; the button is the explicit user action --
   *  a legal acceptance is never recorded silently on the user's behalf. */
  async function acceptConsent() {
    setConsentBusy(true);
    try {
      const job = await createJob({ kind: 'consent' });
      subscribeJob(job.id, {
        onLog: () => {},
        onDone: (d) => {
          const accepted = (d as { accepted?: boolean }).accepted;
          // the sticky warning has served its purpose once accepted (or
          // found unnecessary) -- take it down, then confirm with an ok
          toast.dismiss('consent-needed');
          toast('ok', t(accepted === false ? 's.toast.consentNone' : 's.toast.consentOk'));
          setConsentNeeded(false);
          setConsentBusy(false);
        },
        onError: (m) => { toast('error', m.slice(0, 160)); setConsentBusy(false); },
        onCancelled: () => setConsentBusy(false),
      });
    } catch (e) {
      toast('error', String(e).slice(0, 160));
      setConsentBusy(false);
    }
  }

  async function runAccountOp(op: () => Promise<unknown>, okKey: Parameters<typeof tf>[0], name: string) {
    try {
      await op();
      toast('ok', tf(okKey, { name }));
      refresh();
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    }
  }

  async function toggleDeleteMode(v: boolean) {
    // optimistic: the toggle answers instantly, the server write follows
    setSession((s) => (s ? { ...s, delete_conversation: v } : s));
    try {
      await setDeleteMode(v);
    } catch (e) {
      setSession((s) => (s ? { ...s, delete_conversation: !v } : s));
      toast('error', String(e).slice(0, 160));
    }
  }

  const addAcc = async () => {
    const name = newAccount.trim();
    if (!name) return;
    setNewAccount('');
    try {
      await addAccount(name);
      toast('ok', tf('s.toast.accountAdded', { name }));
      refresh();
      // a new account always needs signing in -- open the browser right away
      // instead of leaving the user to discover the two-step flow
      signIn();
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    }
  };
  const switchAcc = (name: string) =>
    runAccountOp(() => switchAccount(name), 's.toast.accountSwitched', name);

  /** two-step forget: first click arms the confirm label, second executes */
  const askForget = (name: string) => {
    if (confirmForgetFor === name) {
      setConfirmForgetFor(null);
      runAccountOp(() => deleteAccount(name), 's.toast.accountForgot', name);
    } else {
      setConfirmForgetFor(name);
      window.setTimeout(
        () => setConfirmForgetFor((cur) => (cur === name ? null : cur)),
        3000,
      );
    }
  };

  async function changeBrowser(channel: string) {
    try {
      await setBrowser(channel);
      toast('ok', tf('s.toast.browserSet', { channel }));
      refresh();
    } catch (e) {
      toast('error', String(e).slice(0, 160));
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-[min(1280px,94%)] flex-col gap-5 px-8 py-8">
      {ready && (
        <>
      {/* accounts + environment: stacked full-width rows (user preference:
          one card per row beats the old two-column split) */}
      <Card className="flex flex-col gap-4 p-5">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-[13px] font-semibold text-ink-1">
            <UserRound size={14} strokeWidth={2} className="text-ink-3" />
            {t('s.accounts')}
          </span>
          <div className="flex shrink-0 items-center gap-2">
            {/* playground gates audio (biometric data, BIPA) behind a
                one-time account notice; headless runs never see the site's
                dialog, so the upload fails with 451 until this is accepted */}
            <Button
              busy={consentBusy}
              onClick={acceptConsent}
              disabled={!session?.active}
              variant={consentNeeded ? 'primary' : 'ghost'}
              title={t('s.account.consent.hint')}
            >
              <ShieldCheck size={14} strokeWidth={2} /> {t('s.account.consent')}
            </Button>
            <Button busy={loginBusy} onClick={signIn} disabled={!session?.active}>
              <LogIn size={14} strokeWidth={2} /> {t('s.account.login')}
            </Button>
          </div>
        </div>

        {/* account cards: click to switch; the active card offers forget */}
        {session && session.accounts.length === 0 && (
          <div className="rounded-(--radius-m) border border-dashed border-line-2 px-4 py-6 text-center text-[12.5px] leading-relaxed text-ink-3">
            {t('s.account.empty')}
          </div>
        )}
        {session && session.accounts.length > 0 && (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {(expanded ? session.accounts : session.accounts.slice(0, 6)).map((a) => {
              const isActive = a.name === session.active;
              return (
                <div
                  key={a.name}
                  onClick={() => !isActive && switchAcc(a.name)}
                  title={isActive ? undefined : t('s.account.switch')}
                  className={`flex cursor-pointer flex-col gap-1.5 rounded-(--radius-m) border p-3 transition-colors duration-(--dur-in) ${
                    isActive
                      ? 'border-primary bg-primary-dim'
                      : 'border-line-1 hover:border-line-2 hover:bg-hover'
                  }`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1.5 text-[12.5px] font-semibold text-ink-1">
                      <StatusDot ok={a.has_cookies} />
                      <span className="truncate">
                        {a.name === 'default' ? t('s.account.default') : a.name}
                      </span>
                    </span>
                    {isActive && (
                      <span className="shrink-0 text-[10px] font-medium text-primary">
                        {t('s.activeBadge')}
                      </span>
                    )}
                  </div>
                  <span className="text-[10.5px] text-ink-3">
                    {a.has_cookies
                      ? a.updated
                        ? tf('s.account.savedAt', { time: a.updated })
                        : t('s.session.saved')
                      : t('s.account.noSession')}
                  </span>
                  {isActive && (
                    <button
                      onClick={(e) => { e.stopPropagation(); askForget(a.name); }}
                      className={`mt-0.5 self-start rounded-(--radius-s) border px-2 py-0.5 text-left text-[11px] transition-colors duration-(--dur-in) ${
                        confirmForgetFor === a.name
                          ? 'border-err/50 bg-err/10 font-semibold text-err'
                          : 'border-line-1 text-ink-3 hover:border-err/40 hover:bg-err/10 hover:text-err'
                      }`}
                    >
                      {confirmForgetFor === a.name
                        ? t('s.account.forgetConfirm')
                        : t('s.account.forget')}
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {session && session.accounts.length > 6 && (
          <button
            onClick={() => setExpanded((x) => !x)}
            className="self-start text-[11.5px] text-ink-3 transition-colors hover:text-primary"
          >
            {expanded ? t('s.account.collapse') : tf('s.account.expand', { n: session.accounts.length })}
          </button>
        )}

        {/* add account: name it -> browser opens straight at Microsoft sign-in */}
        <div className="flex gap-2">
          <input
            value={newAccount}
            onChange={(e) => setNewAccount(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && addAcc()}
            placeholder={t('s.account.new')}
            className="glass-input h-9 min-w-0 flex-1 px-3 text-[12.5px] text-ink-1 placeholder:text-ink-3"
          />
          <Button
            variant={session?.accounts.length ? 'ghost' : 'primary'}
            lift
            onClick={addAcc}
            disabled={!newAccount.trim() || loginBusy}
          >
            <Plus size={13} strokeWidth={2} />
            {session?.accounts.length ? t('s.account.add') : t('s.account.loginFirst')}
          </Button>
        </div>
        <p className="text-[11px] leading-relaxed text-ink-3">{t('s.account.hint')}</p>

        {/* leave-no-trace toggle: ON = the finished transcription deletes
            its playground conversation (the historic default behavior);
            OFF = a visible record stays on the site */}
        <div className="mt-auto border-t border-line-1 pt-3">
          <div className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-2 text-[12.5px] text-ink-1">
              <Eraser size={13} strokeWidth={2} className="text-ink-3" />
              {t('s.autodelete')}
            </span>
            <Toggle
              checked={session?.delete_conversation ?? true}
              onChange={toggleDeleteMode}
            />
          </div>
          <p className="mt-1.5 text-[11px] leading-relaxed text-ink-3">
            {t('s.autodelete.hint')}
          </p>
        </div>
      </Card>

      <Card className="flex flex-col gap-3 p-5">
        <span className="text-[13px] font-semibold text-ink-1">{t('s.env')}</span>
        {sys && (
          <>
            <div className="flex items-center gap-3">
              <span className="flex shrink-0 items-center gap-2 text-[12.5px] text-ink-2">
                <Compass size={13} strokeWidth={2} className="text-ink-3" />
                {t('s.browser')}
              </span>
              <span className="ml-auto">
                <Select
                  className="w-40"
                  value={sys.browser}
                  onChange={changeBrowser}
                  options={[
                    { value: 'auto', label: t('s.browser.auto') },
                    { value: 'chrome', label: 'Chrome' },
                    { value: 'msedge', label: 'Edge' },
                    { value: 'chromium', label: t('s.browser.chromium') },
                  ]}
                />
              </span>
            </div>
            <div className="flex items-center gap-2 text-[12.5px] text-ink-2">
              <StatusDot ok={sys.ffmpeg.found} /> ffmpeg
              <span className="truncate font-mono text-[10.5px] text-ink-3">{sys.ffmpeg.path ?? ''}</span>
              {/* the app's own copy or the machine's: a bare path cannot say,
                  and the two builds differ in libraries, so this is the row
                  that tells you which ffmpeg the pipeline will actually run */}
              {sys.ffmpeg.source && (
                <span className="shrink-0 rounded-(--radius-s) bg-sunken px-1.5 py-0.5 text-[10px] text-ink-3">
                  {t(sys.ffmpeg.source === 'bundled' ? 's.env.bundled' : 's.env.path')}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 text-[12.5px] text-ink-2">
              <StatusDot ok={sys.cookie_jar} /> {t('s.session')}
              <span className="text-[11px] text-ink-3">
                {sys.cookie_jar ? t('s.session.saved') : t('s.session.none')}
              </span>
            </div>
            <div className="mt-auto flex items-center justify-between gap-2 text-[11.5px] text-ink-3">
              <span>
                mai2srt v{sys.version} / python {sys.python}
              </span>
              {/* branding IS localized per the author's choice:
                  zh shows the hanzi pen name, en the roman handle */}
              <span className="shrink-0">{t('s.branding')}</span>
            </div>
          </>
        )}
      </Card>

        </>
      )}

      {/* storage library manager (full-width): gates itself on its own
          fetch, so it mounts (and starts loading) with the page */}
      <StorageCard />
    </div>
  );
}
