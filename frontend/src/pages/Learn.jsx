import { useEffect, useRef, useState } from 'react';
import YouTube from 'react-youtube';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import QuizOverlay from '../components/QuizOverlay';
import NotesTab from '../components/NotesTab';
import ChatTab from '../components/ChatTab';
import CodeTab from '../components/CodeTab';

const fmt = (s) => {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
};

export default function Learn() {
  const { user } = useAuth();
  const [lectures, setLectures] = useState([]);
  const [activeLecture, setActiveLecture] = useState(null);
  const [activeChunk, setActiveChunk] = useState(null);
  const [currentTime, setCurrentTime] = useState(0);
  const [tab, setTab] = useState('notes');
  const [quizChunk, setQuizChunk] = useState(null);
  const [summary, setSummary] = useState(null);
  const [palAction, setPalAction] = useState(null);  // { text, tone } — shown as toast for a few seconds
  const [triggeredChunks, setTriggeredChunks] = useState(() => new Set());
  const [completedLectures, setCompletedLectures] = useState(() => new Set());
  const playerRef = useRef(null);
  const timerRef = useRef(null);

  const courseId = user?.current_course_id;

  const [masteryMap, setMasteryMap] = useState({});  // concept_id -> {score, attempts}

  const sortByMastery = (lectures, mMap) => {
    const conceptMastery = (lec) => {
      // Each lecture teaches (at least) one concept via its chunks
      const cid = lec.chunks[0]?.concept_id;
      return cid ? (mMap[cid]?.score ?? -1) : -1;  // -1 means not-yet-attempted
    };
    // Struggling first (score in [0, 0.55)), then not-attempted (-1),
    // then progressing (0.55–0.85), then mastered (>= 0.85) last.
    const bucket = (score) => {
      if (score < 0)     return 1;   // not attempted
      if (score < 0.55)  return 0;   // struggling — bubble to top
      if (score < 0.85)  return 2;   // progressing
      return 3;                       // mastered — sink to bottom
    };
    return [...lectures].sort((a, b) => {
      const ba = bucket(conceptMastery(a));
      const bb = bucket(conceptMastery(b));
      if (ba !== bb) return ba - bb;
      return a.title.localeCompare(b.title);
    });
  };

  const refreshPlaylist = async (preserveActive = true) => {
    if (!courseId) return;
    const data = await api(`/api/playlist/${courseId}`);
    const mMap = Object.fromEntries((data.mastery || []).map(m => [m.id, m]));
    setMasteryMap(mMap);
    // Merge in supplementary lectures from other courses (PRD: cross-course curation)
    const supp = (data.supplementary || []).map(l => ({ ...l, supplementary: true }));
    const merged = [...data.lectures, ...supp];
    const sorted = sortByMastery(merged, mMap);
    setLectures(sorted);
    if (!preserveActive || !activeLecture) {
      if (sorted[0]) {
        setActiveLecture(sorted[0]);
        setActiveChunk(sorted[0].chunks[0]);
      }
    }
  };

  useEffect(() => { refreshPlaylist(false); }, [courseId]);

  // Poll player time — used both to render "current chunk" and to trigger quizzes
  useEffect(() => {
    if (!playerRef.current) return;
    if (timerRef.current) clearInterval(timerRef.current);
    timerRef.current = setInterval(async () => {
      const p = playerRef.current;
      if (!p || typeof p.getCurrentTime !== 'function') return;
      const t = await p.getCurrentTime();
      setCurrentTime(t);

      if (!activeLecture) return;
      // Find the chunk we're inside
      const cur = activeLecture.chunks.find(c => t >= c.start && t < c.end);
      if (cur && cur.id !== activeChunk?.id) setActiveChunk(cur);

      // Fire quiz once when a chunk ends
      for (const ch of activeLecture.chunks) {
        // Small tolerance window: trigger between end - 0.5 and end + 1.5
        if (t >= ch.end - 0.5 && t <= ch.end + 1.5 && !triggeredChunks.has(ch.id)) {
          setTriggeredChunks(prev => new Set(prev).add(ch.id));
          await p.pauseVideo();
          setQuizChunk(ch);
          break;
        }
      }
    }, 1000);
    return () => clearInterval(timerRef.current);
  }, [activeLecture, activeChunk, triggeredChunks]);

  const pickLecture = (lec) => {
    setActiveLecture(lec);
    setActiveChunk(lec.chunks[0]);
    setTriggeredChunks(new Set());
    setSummary(null);
  };

  const jumpToChunk = (ch) => {
    setActiveChunk(ch);
    if (playerRef.current?.seekTo) playerRef.current.seekTo(ch.start, true);
  };

  const showPalAction = (text, tone = 'info') => {
    setPalAction({ text, tone });
    setTimeout(() => setPalAction(null), 6000);
  };

  const completeLecture = async (lec) => {
    if (!lec || completedLectures.has(lec.lecture_id)) return;
    setCompletedLectures(prev => new Set(prev).add(lec.lecture_id));
    // Snapshot the current supplementary count so we can announce any additions
    const preSupp = lectures.filter(l => l.supplementary).length;
    await refreshPlaylist(true);
    // Re-read fresh supplementary count via a second call — cheap and simple
    let addedSupp = 0;
    try {
      const data = await api(`/api/playlist/${courseId}`);
      const nowSupp = (data.supplementary || []).length;
      addedSupp = Math.max(0, nowSupp - preSupp);
    } catch {}
    const msg = addedSupp > 0
      ? `Lecture complete — PAL added ${addedSupp} supplementary lecture${addedSupp > 1 ? 's' : ''} from other courses to help with weak topics.`
      : `Lecture complete — PAL rebuilt your playlist based on your performance across "${lec.title}".`;
    showPalAction(msg, addedSupp > 0 ? 'warn' : 'info');
  };

  const onQuizClose = async (result) => {
    const ch = quizChunk;
    setQuizChunk(null);
    // Fetch the hobby-flavoured summary right after quiz — implements the "remediation" step
    if (ch) {
      try {
        const s = await api(`/api/quiz/summary/${ch.id}`);
        setSummary({ chunkId: ch.id, ...s });
      } catch {}
    }

    // Lecture-complete check: quizzes fire at every chunk boundary except the final
    // chunk (which has no boundary after it). So if the just-answered chunk was the
    // second-to-last chunk in the lecture, all quizzes have been offered → lecture done.
    if (ch && activeLecture && result) {
      const chunks = activeLecture.chunks;
      const idx = chunks.findIndex(c => c.id === ch.id);
      const isLastQuizChunk = idx === chunks.length - 2;
      if (isLastQuizChunk) {
        // Defer the completion action until after the intervention plays out below
        setTimeout(() => completeLecture(activeLecture), 800);
      }
    }

    // Act on the Process KG intervention. This is what makes the loop visible.
    const action = result?.intervention?.action;
    const wasCorrect = result?.correct;
    const player = playerRef.current;
    if (!ch || !player) {
      if (player?.playVideo) player.playVideo();
      return;
    }

    // If the answer was correct, never rewind — mastery may still be in the Struggling
    // band while it recovers, but rewinding after a correct answer is pedagogically wrong.
    // The only "positive" action we honour on correct is skip-forward when mastered.
    if (wasCorrect) {
      if (action === 'skip_next_similar_chunk') {
        const chunks = activeLecture?.chunks || [];
        const idx = chunks.findIndex(c => c.id === ch.id);
        const skipTarget = chunks.slice(idx + 1).find(c => c.concept_id === ch.concept_id);
        if (skipTarget) {
          setActiveChunk(skipTarget);
          player.seekTo(skipTarget.end, true);
          setTriggeredChunks(prev => new Set(prev).add(skipTarget.id));
          player.playVideo();
          showPalAction('PAL: you\'ve mastered this — skipped past the redundant section.', 'ok');
          return;
        }
      }
      player.playVideo();
      if (action === 'raise_question_difficulty') {
        showPalAction('Nice one — next quiz will step up a notch.', 'ok');
      } else {
        showPalAction('Correct — continuing.', 'ok');
      }
      return;
    }

    // Wrong answer path — apply the Process KG remediation action.
    switch (action) {
      case 'simplify_with_hobby_analogy':
        player.seekTo(ch.start, true);
        player.pauseVideo();
        showPalAction('PAL: rewound to the start — see the hobby analogy on the right →', 'warn');
        break;
      case 'insert_prerequisite_video':
        player.seekTo(ch.start, true);
        player.playVideo();
        showPalAction('PAL: reviewing this section — replaying from the start.', 'warn');
        break;
      case 'raise_question_difficulty':
        player.playVideo();
        showPalAction('PAL: you\'re confident — next quiz will be harder.', 'ok');
        break;
      case 'skip_next_similar_chunk': {
        // Find the next chunk in the same lecture that teaches the same concept
        const chunks = activeLecture?.chunks || [];
        const idx = chunks.findIndex(c => c.id === ch.id);
        const skipTarget = chunks.slice(idx + 1).find(c => c.concept_id === ch.concept_id);
        if (skipTarget) {
          setActiveChunk(skipTarget);
          player.seekTo(skipTarget.end, true);  // jump past the redundant chunk
          setTriggeredChunks(prev => new Set(prev).add(skipTarget.id));  // don't quiz it either
          player.playVideo();
          showPalAction('PAL: you\'ve mastered this — skipped past the redundant section.', 'ok');
        } else {
          player.playVideo();
          showPalAction('PAL: you\'ve mastered this concept — carrying on.', 'ok');
        }
        break;
      }
      case 'continue_normal':
      default:
        player.playVideo();
        break;
    }
  };

  if (!lectures.length) return <div className="center-full">Loading playlist…</div>;

  const opts = {
    playerVars: { modestbranding: 1, rel: 0 },
  };

  return (
    <div className="learn-shell">
      <div className="playlist-panel">
        <h3>Your Playlist</h3>
        {lectures.map(lec => {
          const cid = lec.chunks[0]?.concept_id;
          const score = cid ? masteryMap[cid]?.score : undefined;
          let badge = null, dim = false;
          if (lec.supplementary) {
            badge = <span className="lec-badge suppl">📚 Bridge lecture</span>;
          } else if (score !== undefined) {
            if (score < 0.55) badge = <span className="lec-badge focus">🎯 Focus</span>;
            else if (score >= 0.85) { badge = <span className="lec-badge mastered">✓ Mastered</span>; dim = true; }
            else badge = <span className="lec-badge progress">{Math.round(score * 100)}%</span>;
          }
          const durLabel = lec.duration_sec ? fmt(lec.duration_sec) : null;
          return (
          <div
            key={lec.lecture_id}
            className={`playlist-item ${lec.lecture_id === activeLecture?.lecture_id ? 'active' : ''} ${dim ? 'dim' : ''} ${lec.supplementary ? 'suppl' : ''}`}
          >
            <div className="lec-title" onClick={() => pickLecture(lec)}>
              <span>
                {lec.supplementary && <span className="play-icon">▶</span>}
                {lec.title}
                {durLabel && <span className="lec-duration"> · {durLabel}</span>}
              </span>
              {badge}
            </div>
            {lec.supplementary && (
              <div className="suppl-meta" onClick={() => pickLecture(lec)}>
                PAL recommended this to strengthen <b>{lec.for_concept_name}</b>
                {typeof lec.similarity === 'number' && (
                  <span className="suppl-sim"> · {Math.round(lec.similarity * 100)}% match</span>
                )}
                <div className="suppl-source">Source: {lec.source_course_title}</div>
              </div>
            )}
            {lec.lecture_id === activeLecture?.lecture_id &&
              lec.chunks.map(ch => (
                <div
                  key={ch.id}
                  className={`chunk ${ch.id === activeChunk?.id ? 'current' : ''}`}
                  onClick={() => jumpToChunk(ch)}
                >
                  ▸ {ch.concept_name} · {fmt(ch.start)}–{fmt(ch.end)}
                </div>
              ))}
          </div>
          );
        })}
      </div>

      <div className="player-panel">
        {activeLecture && (
          <>
            <div className="video-wrap">
              <YouTube
                videoId={activeLecture.youtube_id}
                opts={opts}
                onReady={e => (playerRef.current = e.target)}
                onEnd={() => completeLecture(activeLecture)}
                iframeClassName=""
                style={{ width: '100%', height: '100%' }}
              />
            </div>
            <h2>{activeLecture.title}</h2>
            {activeChunk && (
              <>
                <span className="concept-tag">{activeChunk.concept_name}</span>
                <p className="muted">{activeChunk.summary}</p>
              </>
            )}

            {summary && summary.chunkId === activeChunk?.id && (
              <div className="card" style={{ marginTop: 12 }}>
                <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>PAL · post-chunk summary</div>
                <p style={{ marginTop: 0 }}>{summary.summary}</p>
                {summary.analogy && (
                  <p style={{ borderLeft: '3px solid var(--primary)', paddingLeft: 10, color: 'var(--muted)' }}>
                    <b>Analogy:</b> {summary.analogy}
                  </p>
                )}
                <p style={{ marginBottom: 0 }}><b>Next focus:</b> {summary.next_focus}</p>
              </div>
            )}
          </>
        )}
      </div>

      <div className="sidebar-panel">
        <div className="sidebar-tabs">
          <button className={tab === 'notes' ? 'active' : ''} onClick={() => setTab('notes')}>Notes</button>
          <button className={tab === 'chat' ? 'active' : ''} onClick={() => setTab('chat')}>Chat</button>
          <button className={tab === 'code' ? 'active' : ''} onClick={() => setTab('code')}>Code</button>
        </div>
        <div className="sidebar-body">
          {tab === 'notes' && <NotesTab videoId={activeLecture?.lecture_id} currentTime={currentTime} />}
          {tab === 'chat' && <ChatTab lectureId={activeLecture?.lecture_id} chunkId={activeChunk?.id} />}
          {tab === 'code' && <CodeTab />}
        </div>
      </div>

      {palAction && (
        <div className={`pal-toast pal-toast-${palAction.tone}`}>
          {palAction.text}
        </div>
      )}

      {quizChunk && (
        <QuizOverlay
          lectureId={activeLecture.lecture_id}
          chunk={quizChunk}
          onClose={onQuizClose}
        />
      )}
    </div>
  );
}