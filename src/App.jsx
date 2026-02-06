import React, { useState, useEffect } from 'react';
import {
  Shield,
  Download,
  RefreshCw,
  Linkedin,
  Facebook,
  Twitter,
  CheckCircle2,
  Layout,
  FlaskConical,
  Beaker,
  Stethoscope,
  Info,
} from 'lucide-react';

const App = () => {
  const [activePlatform, setActivePlatform] = useState('linkedin');
  const [imageUrl, setImageUrl] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const apiKey = '';

  const platforms = {
    linkedin: {
      name: 'LinkedIn Professional',
      icon: <Linkedin size={18} />,
      aspect: 'aspect-[4/1]',
      dimensions: '1584 x 396',
      description: 'Optimized for B2B credibility and professional networking.',
      prompt:
        'A professional LinkedIn header banner for CannaHealthUK. High-tech medical laboratory environment, extremely sharp focus on high-end scientific equipment, doctors in white coats discussing data. White clinical space on the left for the CannaHealthUK emerald green leaf logo and serif font branding. Deep navy and emerald green accent colors. Professional, high-authority, 8k resolution.',
    },
    facebook: {
      name: 'Facebook Community',
      icon: <Facebook size={18} />,
      aspect: 'aspect-[851/315]',
      dimensions: '851 x 315',
      description: 'Designed for patient trust and community engagement.',
      prompt:
        "A Facebook cover banner for CannaHealthUK. Modern bright medical laboratory background. Left-aligned branding box with white background, emerald green cannabis leaf logo, and 'Your Health, Our Priority' tagline. High-resolution pharmaceutical aesthetic.",
    },
    twitter: {
      name: 'Twitter/X Newsroom',
      icon: <Twitter size={18} />,
      aspect: 'aspect-[3/1]',
      dimensions: '1500 x 500',
      description: 'Sleek, fast-paced aesthetic for industry updates.',
      prompt:
        'A sleek Twitter header for CannaHealthUK. Abstract medical science patterns combined with realistic lab imagery. Clean white space on the far left for branding. High contrast, clinical lighting, emerald green highlights.',
    },
  };

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const generateAsset = async () => {
    setLoading(true);
    setError(null);
    setImageUrl(null);

    const payload = {
      instances: { prompt: platforms[activePlatform].prompt },
      parameters: { sampleCount: 1 },
    };

    const url = `https://generativelanguage.googleapis.com/v1beta/models/imagen-4.0-generate-001:predict?key=${apiKey}`;

    let attempts = 0;
    while (attempts < 5) {
      try {
        const response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });

        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }

        const result = await response.json();
        const base64Data = result.predictions?.[0]?.bytesBase64Encoded;

        if (base64Data) {
          setImageUrl(`data:image/png;base64,${base64Data}`);
          setLoading(false);
          return;
        }

        throw new Error('Empty response from imaging engine.');
      } catch (err) {
        attempts += 1;
        if (attempts >= 5) {
          setError('System saturation detected. Please retry in a few moments.');
          setLoading(false);
        } else {
          await sleep(Math.pow(2, attempts) * 1000);
        }
      }
    }
  };

  useEffect(() => {
    generateAsset();
  }, [activePlatform]);

  return (
    <div className="min-h-screen bg-[#F8FAFC] font-sans text-slate-900 p-4 md:p-8">
      <div className="max-w-6xl mx-auto">
        <header className="flex flex-col md:flex-row justify-between items-start md:items-center mb-8 gap-4">
          <div className="flex items-center gap-4">
            <div className="bg-emerald-600 p-3 rounded-2xl shadow-lg shadow-emerald-200">
              <Shield className="text-white" size={28} />
            </div>
            <div>
              <h1 className="text-2xl font-bold tracking-tight text-slate-800">
                CannaHealthUK Identity Hub
              </h1>
              <p className="text-slate-500 text-sm font-medium flex items-center gap-2">
                <span className="w-2 h-2 bg-emerald-500 rounded-full animate-pulse"></span>
                Multi-Platform Brand Orchestrator Active
              </p>
            </div>
          </div>

          <div className="flex bg-white p-1 rounded-xl shadow-sm border border-slate-200">
            {Object.entries(platforms).map(([id, platform]) => (
              <button
                key={id}
                onClick={() => setActivePlatform(id)}
                className={`flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-bold transition-all ${
                  activePlatform === id
                    ? 'bg-slate-900 text-white shadow-md'
                    : 'text-slate-500 hover:text-slate-800 hover:bg-slate-50'
                }`}
              >
                {platform.icon}
                <span className="hidden sm:inline">{platform.name.split(' ')[0]}</span>
              </button>
            ))}
          </div>
        </header>

        <div className="grid grid-cols-1 lg:grid-cols-4 gap-8">
          <div className="lg:col-span-3 space-y-6">
            <div className="bg-white rounded-3xl shadow-xl shadow-slate-200/50 border border-slate-200 overflow-hidden">
              <div className="p-6 border-b border-slate-100 flex justify-between items-center">
                <div className="flex items-center gap-3">
                  <div className="p-2 bg-slate-100 rounded-lg text-slate-500">
                    <Layout size={20} />
                  </div>
                  <div>
                    <h2 className="text-sm font-black uppercase tracking-[0.15em] text-slate-400">
                      Preview Canvas
                    </h2>
                    <p className="text-xs text-slate-500">
                      {platforms[activePlatform].dimensions} px
                    </p>
                  </div>
                </div>
                <button
                  onClick={generateAsset}
                  disabled={loading}
                  className="p-2 text-emerald-600 hover:bg-emerald-50 rounded-full transition-colors disabled:opacity-30"
                >
                  <RefreshCw className={loading ? 'animate-spin' : ''} size={20} />
                </button>
              </div>

              <div
                className={`relative w-full ${platforms[activePlatform].aspect} bg-slate-50 flex items-center justify-center group`}
              >
                {loading ? (
                  <div className="flex flex-col items-center gap-6">
                    <div className="relative">
                      <div className="w-20 h-20 border-4 border-emerald-100 rounded-full"></div>
                      <div className="w-20 h-20 border-4 border-emerald-600 border-t-transparent rounded-full animate-spin absolute top-0 left-0"></div>
                      <FlaskConical
                        className="absolute top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2 text-emerald-600"
                        size={24}
                      />
                    </div>
                    <div className="text-center">
                      <p className="text-lg font-bold text-slate-700 animate-pulse">
                        Orchestrating Asset...
                      </p>
                      <p className="text-xs text-slate-400 uppercase tracking-widest mt-1">
                        Applying {activePlatform} Standards
                      </p>
                    </div>
                  </div>
                ) : imageUrl ? (
                  <>
                    <img
                      src={imageUrl}
                      alt="Brand Asset"
                      className="w-full h-full object-cover transition-opacity duration-1000"
                    />
                    <div className="absolute inset-0 bg-slate-900/60 opacity-0 group-hover:opacity-100 transition-all flex flex-col items-center justify-center backdrop-blur-sm">
                      <p className="text-white font-bold mb-4 tracking-wide uppercase text-xs">
                        Ready for Deployment
                      </p>
                      <button
                        onClick={() => {
                          const link = document.createElement('a');
                          link.href = imageUrl;
                          link.download = `CannaHealthUK_${activePlatform}_Banner.png`;
                          link.click();
                        }}
                        className="bg-white text-slate-900 px-8 py-3 rounded-2xl font-black flex items-center gap-3 shadow-2xl hover:scale-105 active:scale-95 transition-all"
                      >
                        <Download size={20} />
                        Download Asset
                      </button>
                    </div>
                  </>
                ) : (
                  <div className="text-center p-12">
                    <Beaker size={48} className="mx-auto text-slate-300 mb-4" />
                    <p className="text-slate-400 font-medium">{error || 'System Idle'}</p>
                  </div>
                )}
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="bg-white p-6 rounded-3xl border border-slate-200 shadow-sm">
                <h3 className="text-xs font-black text-slate-400 uppercase tracking-[0.2em] mb-4 flex items-center gap-2">
                  <Stethoscope size={16} /> Asset Objectives
                </h3>
                <p className="text-sm text-slate-600 leading-relaxed mb-4">
                  {platforms[activePlatform].description}
                </p>
                <div className="space-y-3">
                  <div className="flex items-start gap-3 text-xs text-slate-500">
                    <CheckCircle2
                      size={14}
                      className="text-emerald-500 shrink-0 mt-0.5"
                    />
                    <span>Clinical whitespace preserved for branding clarity.</span>
                  </div>
                  <div className="flex items-start gap-3 text-xs text-slate-500">
                    <CheckCircle2
                      size={14}
                      className="text-emerald-500 shrink-0 mt-0.5"
                    />
                    <span>Laboratory-first imagery to signal R&amp;D excellence.</span>
                  </div>
                </div>
              </div>

              <div className="bg-[#111827] p-6 rounded-3xl text-white shadow-xl relative overflow-hidden">
                <div className="relative z-10">
                  <h3 className="text-xs font-black text-slate-400 uppercase tracking-[0.2em] mb-4">
                    AI Vision Prompt
                  </h3>
                  <div className="bg-white/10 p-4 rounded-xl text-xs font-mono text-slate-300 leading-relaxed border border-white/10 italic">
                    {platforms[activePlatform].prompt}
                  </div>
                </div>
                <div className="absolute -bottom-10 -right-10 w-40 h-40 bg-emerald-500/20 rounded-full blur-3xl"></div>
              </div>
            </div>
          </div>

          <div className="space-y-6">
            <div className="bg-white p-6 rounded-3xl border border-slate-200 shadow-sm">
              <h3 className="text-sm font-bold text-slate-800 mb-6 flex items-center gap-2">
                <Info size={18} className="text-emerald-600" /> System Metrics
              </h3>

              <div className="space-y-6">
                <div className="space-y-2">
                  <div className="flex justify-between text-xs font-bold uppercase tracking-wider text-slate-400">
                    <span>Clinical Authority</span>
                    <span className="text-emerald-600">98%</span>
                  </div>
                  <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                    <div className="h-full bg-emerald-500 w-[98%]"></div>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="flex justify-between text-xs font-bold uppercase tracking-wider text-slate-400">
                    <span>Patient Trust</span>
                    <span className="text-emerald-600">94%</span>
                  </div>
                  <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                    <div className="h-full bg-emerald-500 w-[94%]"></div>
                  </div>
                </div>

                <div className="space-y-2">
                  <div className="flex justify-between text-xs font-bold uppercase tracking-wider text-slate-400">
                    <span>R&amp;D Precision</span>
                    <span className="text-emerald-600">100%</span>
                  </div>
                  <div className="h-1.5 w-full bg-slate-100 rounded-full overflow-hidden">
                    <div className="h-full bg-emerald-500 w-full"></div>
                  </div>
                </div>
              </div>

              <div className="mt-8 pt-6 border-t border-slate-100">
                <p className="text-[10px] text-slate-400 leading-relaxed">
                  <strong>Aurora Protocol:</strong> These assets are generated in
                  high-fidelity to ensure CannaHealthUK is perceived as a
                  pharmaceutical-grade leader, not a lifestyle startup.
                </p>
              </div>
            </div>

            <div className="bg-emerald-50 p-6 rounded-3xl border border-emerald-100 flex flex-col items-center text-center">
              <div className="bg-white p-3 rounded-2xl shadow-sm mb-4">
                <Beaker className="text-emerald-600" size={24} />
              </div>
              <h4 className="text-emerald-900 font-bold mb-2">Need a different size?</h4>
              <p className="text-emerald-700/70 text-xs mb-4 leading-relaxed">
                I can add custom dimensions for Instagram, YouTube, or physical
                print media.
              </p>
              <button className="text-xs font-black text-emerald-600 uppercase tracking-widest hover:underline transition-all">
                Request Custom Dimension
              </button>
            </div>
          </div>
        </div>
      </div>

      <footer className="mt-12 text-center text-slate-400 text-[10px] uppercase tracking-[0.3em]">
        Aurora Brand Orchestrator • Version 2.0 • Unified Clinical Framework
      </footer>
    </div>
  );
};

export default App;
