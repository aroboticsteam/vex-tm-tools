import { useCallback, useEffect, useRef, useState } from 'react';
import { Camera, Circle, RefreshCw, Square, Upload } from 'lucide-react';
import { useTeams } from '../api/teams';
import { useTeamClips, useUploadClip } from '../api/clips';
import { usePermission } from '../hooks/usePermission';
import { useUiStore } from '../stores/ui';
import { StatusBadge } from '../components/ui/Badge';
import { Button } from '../components/ui/Button';
import { Card, PageHeader } from '../components/ui/Card';
import { Input, Label, Select } from '../components/ui/Input';
import {
  DEFAULT_CLIP_SECONDS,
  MAX_CLIP_SECONDS,
  MIN_CLIP_SECONDS,
  OUTPUT_SIZE,
  TARGET_FPS,
  clampSeconds,
  extensionFor,
  isValidTeamNumber,
  pickMimeType,
  squareCrop,
} from '../lib/recorder';
import type { ClipKind } from '../types/api';

type Phase = 'idle' | 'countdown' | 'recording' | 'review';

const KIND_LABEL: Record<ClipKind, string> = { bot: 'Robot (in front of green screen)', team: 'Team (in front of green screen)' };

/** Records a short square, 60fps clip from a webcam / capture card straight
 * in the browser, then uploads it for green-screen keying (issues #1/#12).
 * Two clips per team: the bot and the team. */
export function RecorderPage() {
  const canUpload = usePermission('video:upload');
  const { data: teams } = useTeams();
  const pushToast = useUiStore((s) => s.pushToast);
  const upload = useUploadClip();

  const [team, setTeam] = useState('');
  const [kind, setKind] = useState<ClipKind>('bot');
  const [seconds, setSeconds] = useState(DEFAULT_CLIP_SECONDS);
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const [deviceId, setDeviceId] = useState('');
  const [phase, setPhase] = useState<Phase>('idle');
  const [countdown, setCountdown] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [cameraFps, setCameraFps] = useState<number | null>(null);
  const [cameraError, setCameraError] = useState<string | null>(null);
  const [recording, setRecording] = useState<{ blob: Blob; url: string; mime: string } | null>(null);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const rafRef = useRef<number | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const timersRef = useRef<number[]>([]);

  const validTeam = isValidTeamNumber(team);
  const { data: clips } = useTeamClips(validTeam ? team.trim().toUpperCase() : null);

  const stopCamera = useCallback(() => {
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  // Paints the centred square crop of the camera into the canvas every frame;
  // the canvas is what actually gets recorded (so the clip is square).
  const draw = useCallback(() => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (video && canvas && video.videoWidth > 0) {
      const { sx, sy, size } = squareCrop(video.videoWidth, video.videoHeight);
      canvas.getContext('2d')?.drawImage(video, sx, sy, size, size, 0, 0, OUTPUT_SIZE, OUTPUT_SIZE);
    }
    rafRef.current = requestAnimationFrame(draw);
  }, []);

  const startCamera = useCallback(
    async (id: string) => {
      stopCamera();
      setCameraError(null);
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: {
            ...(id ? { deviceId: { exact: id } } : {}),
            width: { ideal: 1920 },
            height: { ideal: 1080 },
            frameRate: { ideal: TARGET_FPS, min: 30 },
          },
        });
        streamRef.current = stream;
        setCameraFps(stream.getVideoTracks()[0]?.getSettings().frameRate ?? null);
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play().catch(() => undefined);
        }
        setDevices((await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === 'videoinput'));
        rafRef.current = requestAnimationFrame(draw);
      } catch (err) {
        setCameraError(err instanceof Error ? err.message : 'Could not access the camera');
      }
    },
    [draw, stopCamera],
  );

  useEffect(() => {
    if (phase !== 'review') void startCamera(deviceId);
    return undefined;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deviceId]);

  useEffect(
    () => () => {
      stopCamera();
      timersRef.current.forEach((t) => window.clearTimeout(t));
      timersRef.current.forEach((t) => window.clearInterval(t));
    },
    [stopCamera],
  );

  function startRecording() {
    const canvas = canvasRef.current;
    if (!canvas || !streamRef.current) return;
    const mime = pickMimeType((m) => typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(m));
    if (!mime) {
      pushToast('This browser cannot record video', 'error');
      return;
    }
    const chunks: Blob[] = [];
    const recorder = new MediaRecorder(canvas.captureStream(TARGET_FPS), { mimeType: mime, videoBitsPerSecond: 16_000_000 });
    recorder.ondataavailable = (e) => e.data.size > 0 && chunks.push(e.data);
    recorder.onstop = () => {
      const blob = new Blob(chunks, { type: mime });
      setRecording({ blob, url: URL.createObjectURL(blob), mime });
      setPhase('review');
      stopCamera();
    };
    recorderRef.current = recorder;

    setPhase('countdown');
    let remaining = 3;
    setCountdown(remaining);
    const tick = window.setInterval(() => {
      remaining -= 1;
      setCountdown(remaining);
      if (remaining > 0) return;
      window.clearInterval(tick);
      recorder.start(250);
      setPhase('recording');
      const startedAt = performance.now();
      const clock = window.setInterval(() => setElapsed((performance.now() - startedAt) / 1000), 100);
      const stopTimer = window.setTimeout(() => {
        window.clearInterval(clock);
        setElapsed(0);
        if (recorder.state !== 'inactive') recorder.stop();
      }, clampSeconds(seconds) * 1000);
      timersRef.current.push(clock, stopTimer);
    }, 1000);
    timersRef.current.push(tick);
  }

  function stopEarly() {
    timersRef.current.forEach((t) => window.clearTimeout(t));
    if (recorderRef.current && recorderRef.current.state !== 'inactive') recorderRef.current.stop();
  }

  function retake() {
    if (recording) URL.revokeObjectURL(recording.url);
    setRecording(null);
    setPhase('idle');
    void startCamera(deviceId);
  }

  function submit() {
    if (!recording || !validTeam) return;
    const number = team.trim().toUpperCase();
    const file = new File([recording.blob], `${number}-${kind}.${extensionFor(recording.mime)}`, { type: recording.mime });
    upload.mutate(
      { number, kind, file, keyColour: '#00B140', similarity: 0.1, blend: 0.05 },
      {
        onSuccess: () => {
          pushToast(`${number} ${kind} clip uploaded — keying in progress`, 'success');
          retake();
        },
        onError: (err) => pushToast(err instanceof Error ? err.message : 'Upload failed', 'error'),
      },
    );
  }

  const busy = phase === 'countdown' || phase === 'recording';

  return (
    <div>
      <PageHeader
        title="Record clips"
        subtitle="Record a short square clip of a bot or team in front of the green screen. It's keyed to a transparent video for the stream graphics."
      />
      {!canUpload && <p className="mb-3 text-sm text-vmd-danger">You need the video upload permission to record clips.</p>}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card>
          <div className="relative mx-auto aspect-square w-full max-w-xl overflow-hidden rounded-xl bg-black">
            {/* Hidden source element; the visible, recorded picture is the cropped canvas. */}
            <video ref={videoRef} className="hidden" playsInline muted />
            {phase === 'review' && recording ? (
              <video src={recording.url} className="h-full w-full object-contain" controls loop autoPlay playsInline />
            ) : (
              <canvas ref={canvasRef} width={OUTPUT_SIZE} height={OUTPUT_SIZE} className="h-full w-full object-contain" />
            )}
            {phase === 'countdown' && (
              <div className="absolute inset-0 flex items-center justify-center bg-black/40 text-8xl font-bold text-white">
                {countdown}
              </div>
            )}
            {phase === 'recording' && (
              <div className="absolute left-3 top-3 flex items-center gap-2 rounded-full bg-black/70 px-3 py-1 text-sm text-white">
                <Circle size={12} className="animate-pulse fill-red-500 text-red-500" /> {elapsed.toFixed(1)}s / {clampSeconds(seconds)}s
              </div>
            )}
          </div>
          {cameraError && <p className="mt-2 text-sm text-vmd-danger">{cameraError}</p>}
          {cameraFps != null && phase !== 'review' && (
            <p className="mt-2 text-xs text-vmd-textSubtle">
              Camera reports ~{Math.round(cameraFps)} fps{cameraFps < 59 ? ' — lower than 60, so slow-motion will look choppier' : ''}.
            </p>
          )}
          <div className="mt-3 flex flex-wrap justify-center gap-2">
            {phase === 'review' ? (
              <>
                <Button variant="secondary" onClick={retake}>
                  <RefreshCw size={14} /> Retake
                </Button>
                <Button variant="primary" onClick={submit} disabled={!validTeam || !canUpload || upload.isPending}>
                  <Upload size={14} /> {upload.isPending ? 'Uploading…' : 'Upload clip'}
                </Button>
              </>
            ) : busy ? (
              <Button variant="danger" onClick={stopEarly} disabled={phase === 'countdown'}>
                <Square size={14} /> Stop
              </Button>
            ) : (
              <Button variant="primary" onClick={startRecording} disabled={!validTeam || !canUpload || !!cameraError}>
                <Camera size={14} /> Record
              </Button>
            )}
          </div>
        </Card>

        <div className="space-y-4">
          <Card>
            <div className="space-y-3">
              <div>
                <Label>Team number</Label>
                <Input list="recorder-teams" value={team} onChange={(e) => setTeam(e.target.value.toUpperCase())} placeholder="21549A" disabled={busy} />
                <datalist id="recorder-teams">
                  {(teams ?? []).map((t) => (
                    <option key={t.team_number} value={t.team_number} />
                  ))}
                </datalist>
              </div>
              <div>
                <Label>Clip</Label>
                <Select value={kind} onChange={(e) => setKind(e.target.value as ClipKind)} disabled={busy}>
                  <option value="bot">{KIND_LABEL.bot}</option>
                  <option value="team">{KIND_LABEL.team}</option>
                </Select>
              </div>
              <div>
                <Label>
                  Length: {clampSeconds(seconds)}s ({MIN_CLIP_SECONDS}–{MAX_CLIP_SECONDS})
                </Label>
                <input
                  type="range"
                  min={MIN_CLIP_SECONDS}
                  max={MAX_CLIP_SECONDS}
                  value={seconds}
                  onChange={(e) => setSeconds(Number(e.target.value))}
                  disabled={busy}
                  className="w-full"
                />
              </div>
              <div>
                <Label>Camera</Label>
                <Select value={deviceId} onChange={(e) => setDeviceId(e.target.value)} disabled={busy || phase === 'review'}>
                  <option value="">Default camera</option>
                  {devices.map((d, i) => (
                    <option key={d.deviceId || i} value={d.deviceId}>
                      {d.label || `Camera ${i + 1}`}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
          </Card>
          {validTeam && (
            <Card>
              <p className="mb-2 text-sm font-medium text-vmd-textStrong">{team.trim().toUpperCase()} clips</p>
              {(['bot', 'team'] as ClipKind[]).map((k) => {
                const c = clips?.clips[k];
                return (
                  <div key={k} className="flex items-center justify-between py-1 text-sm">
                    <span className="text-vmd-textMuted">{k === 'bot' ? 'Robot' : 'Team'}</span>
                    <span title={c?.error ?? undefined}>
                      <StatusBadge status={c?.status ?? 'NONE'} />
                    </span>
                  </div>
                );
              })}
            </Card>
          )}
        </div>
      </div>
    </div>
  );
}
