// Dr. Marcus Chen - Crypto Trading Expert UI

class CryptoExpertUI {
    constructor() {
        this.chatMessages = document.getElementById('chat-messages');
        this.chatForm = document.getElementById('chat-form');
        this.chatInput = document.getElementById('chat-input');
        this.sendBtn = document.getElementById('send-btn');
        this.menuToggle = document.getElementById('menu-toggle');
        this.sidebar = document.querySelector('.sidebar');

        this.isTyping = false;
        this.init();
    }

    init() {
        this.setupEventListeners();
        this.updateMarketData();
        this.updateFearGreedIndex();
        this.createOverlay();

        // Refresh market data periodically
        setInterval(() => this.updateMarketData(), 30000);
    }

    setupEventListeners() {
        // Form submission
        this.chatForm.addEventListener('submit', (e) => {
            e.preventDefault();
            this.handleSubmit();
        });

        // Quick action buttons
        document.querySelectorAll('.quick-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                const query = btn.dataset.query;
                this.chatInput.value = query;
                this.handleSubmit();
            });
        });

        // Mobile menu toggle
        this.menuToggle.addEventListener('click', () => {
            this.toggleSidebar();
        });

        // Input focus effects
        this.chatInput.addEventListener('focus', () => {
            this.chatInput.parentElement.classList.add('focused');
        });

        this.chatInput.addEventListener('blur', () => {
            this.chatInput.parentElement.classList.remove('focused');
        });
    }

    createOverlay() {
        const overlay = document.createElement('div');
        overlay.className = 'overlay';
        overlay.addEventListener('click', () => this.toggleSidebar());
        document.body.appendChild(overlay);
        this.overlay = overlay;
    }

    toggleSidebar() {
        this.sidebar.classList.toggle('active');
        this.overlay.classList.toggle('active');
    }

    handleSubmit() {
        const message = this.chatInput.value.trim();
        if (!message || this.isTyping) return;

        this.addMessage(message, 'user');
        this.chatInput.value = '';
        this.generateResponse(message);
    }

    addMessage(content, type, isHTML = false) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${type}`;

        const avatar = type === 'user' ? 'You' : 'MC';
        const author = type === 'user' ? 'You' : 'Dr. Marcus Chen';
        const time = this.formatTime(new Date());

        messageDiv.innerHTML = `
            <div class="message-avatar">${avatar}</div>
            <div class="message-content">
                <div class="message-header">
                    <span class="message-author">${author}</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-text">
                    ${isHTML ? content : `<p>${this.escapeHTML(content)}</p>`}
                </div>
            </div>
        `;

        this.chatMessages.appendChild(messageDiv);
        this.scrollToBottom();
    }

    addTypingIndicator() {
        const typingDiv = document.createElement('div');
        typingDiv.className = 'message assistant typing-message';
        typingDiv.innerHTML = `
            <div class="message-avatar">MC</div>
            <div class="message-content">
                <div class="message-text">
                    <div class="typing-indicator">
                        <span></span>
                        <span></span>
                        <span></span>
                    </div>
                </div>
            </div>
        `;
        this.chatMessages.appendChild(typingDiv);
        this.scrollToBottom();
        return typingDiv;
    }

    async generateResponse(userMessage) {
        this.isTyping = true;
        this.sendBtn.disabled = true;

        const typingIndicator = this.addTypingIndicator();

        // Simulate thinking time
        await this.delay(1500 + Math.random() * 1500);

        // Remove typing indicator
        typingIndicator.remove();

        // Generate contextual response
        const response = this.getContextualResponse(userMessage);
        this.addMessage(response, 'assistant', true);

        this.isTyping = false;
        this.sendBtn.disabled = false;
    }

    getContextualResponse(message) {
        const lowerMessage = message.toLowerCase();

        // Bitcoin analysis
        if (lowerMessage.includes('bitcoin') || lowerMessage.includes('btc')) {
            return this.getBitcoinAnalysis();
        }

        // Ethereum analysis
        if (lowerMessage.includes('ethereum') || lowerMessage.includes('eth')) {
            return this.getEthereumAnalysis();
        }

        // Solana analysis
        if (lowerMessage.includes('solana') || lowerMessage.includes('sol')) {
            return this.getSolanaAnalysis();
        }

        // Altcoin discussion
        if (lowerMessage.includes('altcoin') || lowerMessage.includes('alt')) {
            return this.getAltcoinAnalysis();
        }

        // Smart money
        if (lowerMessage.includes('smart money') || lowerMessage.includes('whale')) {
            return this.getSmartMoneyAnalysis();
        }

        // Market sentiment
        if (lowerMessage.includes('market') || lowerMessage.includes('sentiment')) {
            return this.getMarketSentiment();
        }

        // Default response
        return this.getDefaultResponse(message);
    }

    getBitcoinAnalysis() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> Bitcoin is showing accumulation patterns at key support levels. On-chain data suggests smart money is positioning for the next leg up while retail remains cautious—exactly the setup I look for.</p>

            <div class="analysis-section">
                <h4>Market Positioning</h4>
                <ul>
                    <li>Current range: Consolidating between $95K-$102K</li>
                    <li>Key support: $94,500 (high volume node)</li>
                    <li>Key resistance: $105,000 (psychological + technical confluence)</li>
                    <li>4H RSI: 52 - neutral with bullish divergence forming</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>On-Chain Intelligence</h4>
                <ul>
                    <li>Exchange outflows accelerating - 25K BTC moved to cold storage this week</li>
                    <li>Long-term holder supply at ATH - diamond hands aren't selling</li>
                    <li>Funding rates slightly negative - shorts getting crowded</li>
                    <li>Miner selling pressure declining post-halving adjustment</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>What most people are missing: The ETF flows tell one story, but the OTC desks tell another. I'm hearing institutional blocks are being accumulated at these levels with minimal slippage. When retail realizes the supply shock that's coming, they'll be chasing.</p>
            </div>

            <div class="analysis-section">
                <h4>Risk Factors</h4>
                <ul>
                    <li>Macro: Fed policy pivot timing remains uncertain</li>
                    <li>A break below $92K invalidates the bull thesis short-term</li>
                    <li>Geopolitical black swans always a possibility</li>
                </ul>
            </div>

            <p><strong>RECOMMENDATION:</strong> Accumulate on dips to $95K-$96K range. Stop loss below $92K. Target: $115K-$120K for this cycle leg.</p>

            <span class="confidence-badge high">High Confidence</span>
        `;
    }

    getEthereumAnalysis() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> Ethereum is at an inflection point. The ETH/BTC ratio has been bleeding, but the risk/reward here is asymmetric. DeFi metrics are quietly improving while everyone's focused on Bitcoin.</p>

            <div class="analysis-section">
                <h4>Market Positioning</h4>
                <ul>
                    <li>Price: Testing $3,400 support zone</li>
                    <li>ETH/BTC: 0.035 - historically oversold territory</li>
                    <li>Weekly RSI: 42 - approaching oversold</li>
                    <li>Major resistance: $4,000 psychological level</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>Fundamental Catalysts</h4>
                <ul>
                    <li>L2 activity hitting new highs - Base and Arbitrum leading</li>
                    <li>ETH burn rate increasing with DeFi resurgence</li>
                    <li>Staking yield remains attractive at 4.2%</li>
                    <li>Pectra upgrade on the horizon - watch for narrative shift</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>Here's what the market's overlooking: Ethereum's "boring" phase is exactly when smart money loads up. The ETH/BTC ratio mean-reverts, and we're seeing accumulation from addresses that historically time bottoms well. The ETF flows have been muted, but that's about to change.</p>
            </div>

            <p><strong>RECOMMENDATION:</strong> Strong buy zone between $3,200-$3,500. This is a spot accumulation play, not a leverage trade. Target: $5,500+ this cycle.</p>

            <span class="confidence-badge medium">Medium-High Confidence</span>
        `;
    }

    getSolanaAnalysis() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> Solana has been one of the strongest performers, but the easy money has been made. Now it's about selective positioning and watching for rotation signals.</p>

            <div class="analysis-section">
                <h4>Market Positioning</h4>
                <ul>
                    <li>Price: Consolidating after recent rally</li>
                    <li>SOL/ETH: Near cycle highs - extended</li>
                    <li>Daily RSI: 58 - neutral but coming off overbought</li>
                    <li>Key level: $180 support must hold</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>Ecosystem Metrics</h4>
                <ul>
                    <li>DEX volume: #1 across all chains consistently</li>
                    <li>NFT activity: Resurging with new collections</li>
                    <li>Developer activity: Strong, but ecosystem concentration risk</li>
                    <li>Memecoin mania: Driving volume but not sustainable</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>The real story here: SOL has become the retail darling, which makes me cautious. When your Uber driver mentions Solana, you're not early anymore. I'm watching for profit-taking rotation into undervalued L1s. Play it smart—don't be exit liquidity.</p>
            </div>

            <p><strong>RECOMMENDATION:</strong> Take profits on 30-40% of position if you're in profit. Hold core position but don't add here. Wait for pullback to $160-$170 for new entries.</p>

            <span class="confidence-badge medium">Medium Confidence</span>
        `;
    }

    getAltcoinAnalysis() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> We're approaching altcoin season territory, but selectivity is crucial. Not all alts will pump equally—this cycle rewards quality and narrative alignment.</p>

            <div class="analysis-section">
                <h4>Sector Breakdown</h4>
                <ul>
                    <li><strong>AI/Compute:</strong> Narrative strong but valuations stretched</li>
                    <li><strong>DePIN:</strong> Under-the-radar accumulation happening</li>
                    <li><strong>RWA:</strong> Institutional favorite, slower but steadier</li>
                    <li><strong>Gaming:</strong> Lagging but catalysts loading</li>
                    <li><strong>L2s:</strong> Value accrual debate ongoing</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>What I'm Watching</h4>
                <ul>
                    <li>Bitcoin dominance: Currently 54% - needs to break down for alt season</li>
                    <li>Altcoin season index: 38/100 - not there yet</li>
                    <li>ETH strength: Usually leads altcoin rotation</li>
                    <li>Stablecoin flows: New money entering via USDC</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>My alpha for this cycle: Focus on tokens with real revenue and upcoming catalysts. The market has matured—pure narrative plays without fundamentals will underperform. I'm positioned in infrastructure plays that benefit regardless of which L1 wins.</p>
            </div>

            <p><strong>RECOMMENDATION:</strong> Build altcoin positions gradually. Don't FOMO into pumped charts. Let Bitcoin consolidate, watch for ETH/BTC ratio reversal, then deploy into quality alts.</p>

            <span class="confidence-badge medium">Medium Confidence</span>
        `;
    }

    getSmartMoneyAnalysis() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> Smart money is playing a different game than retail. While CT debates tops and bottoms, institutions are quietly building positions in assets with clear regulatory pathways.</p>

            <div class="analysis-section">
                <h4>Whale Activity This Week</h4>
                <ul>
                    <li>Large BTC transfers to custody solutions up 40%</li>
                    <li>ETH accumulation wallets adding at $3,300-$3,500</li>
                    <li>Stablecoin reserves on exchanges: $28B - dry powder ready</li>
                    <li>OTC desk activity: Elevated, mostly buy-side</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>Institutional Signals</h4>
                <ul>
                    <li>ETF flows: Consistent daily inflows despite price chop</li>
                    <li>CME open interest: Near ATH - institutions are HERE</li>
                    <li>Corporate treasury allocations: New announcements weekly</li>
                    <li>Sovereign wealth funds: Whispers of position building</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>Here's what separates professionals from amateurs: Smart money isn't trying to time the exact bottom. They're accumulating with a 2-3 year horizon while retail panics over 10% drawdowns. The asymmetry in this market is that institutions have longer time horizons and lower cost of capital. Trade accordingly.</p>
            </div>

            <p><strong>KEY INSIGHT:</strong> Follow the custody flows, not the Twitter sentiment. When coins move to cold storage and institutional custody, that's the real signal.</p>

            <span class="confidence-badge high">High Confidence</span>
        `;
    }

    getMarketSentiment() {
        return `
            <p><strong>EXECUTIVE SUMMARY:</strong> Market sentiment is in the "cautiously optimistic" zone—which historically is a good setup. Not euphoric enough for a top, not fearful enough for capitulation.</p>

            <div class="analysis-section">
                <h4>Sentiment Indicators</h4>
                <ul>
                    <li>Fear & Greed Index: 62 (Greed) - elevated but not extreme</li>
                    <li>Social volume: Declining from recent peaks - healthy</li>
                    <li>Funding rates: Slightly positive - balanced market</li>
                    <li>Google Trends: "Bitcoin" searches moderate</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>Contrarian Signals</h4>
                <ul>
                    <li>CT consensus: Expecting pullback - often wrong at inflections</li>
                    <li>Leverage: Moderate levels - not overheated</li>
                    <li>Retail participation: Still below 2021 levels</li>
                    <li>Media coverage: Neutral to positive</li>
                </ul>
            </div>

            <div class="analysis-section">
                <h4>The Insider Angle</h4>
                <p>The best trades feel uncomfortable. Right now, the "safe" play feels like waiting for a bigger pullback. But the market rarely gives you the entry you want. I've seen this setup before—steady accumulation while everyone waits for a crash that doesn't come. Be positioned, not perfect.</p>
            </div>

            <p><strong>BOTTOM LINE:</strong> Sentiment supports continuation. Use weakness to add, not to panic. The time to be fearful is when your neighbor starts giving crypto advice—we're not there yet.</p>

            <span class="confidence-badge medium">Medium-High Confidence</span>
        `;
    }

    getDefaultResponse(message) {
        return `
            <p>Good question. Let me give you the institutional perspective on this.</p>

            <p>In my 15+ years across traditional and crypto markets, I've learned that <strong>most retail traders overcomplicate their analysis</strong> while missing the simple signals that actually matter.</p>

            <p>To give you a proper analysis, I need a bit more context:</p>

            <div class="analysis-section">
                <h4>Tell Me More About</h4>
                <ul>
                    <li>Which specific asset or sector are you looking at?</li>
                    <li>What's your timeframe? (Swing trade, position trade, long-term hold)</li>
                    <li>What's your risk tolerance and current exposure?</li>
                </ul>
            </div>

            <p>Once I understand your situation better, I can provide the kind of analysis that separates professional traders from the crowd. The devil is always in the details.</p>

            <p>Ask me about <strong>Bitcoin, Ethereum, Solana, altcoin positioning,</strong> or <strong>what smart money is doing</strong>—and I'll give you the full breakdown.</p>
        `;
    }

    updateMarketData() {
        // Simulated market data with realistic ranges
        const btcPrice = 97500 + (Math.random() - 0.5) * 4000;
        const ethPrice = 3420 + (Math.random() - 0.5) * 200;
        const solPrice = 195 + (Math.random() - 0.5) * 20;

        const btcChange = (Math.random() - 0.4) * 5;
        const ethChange = (Math.random() - 0.45) * 6;
        const solChange = (Math.random() - 0.5) * 7;

        this.updatePriceElement('btc-price', btcPrice, true);
        this.updatePriceElement('eth-price', ethPrice, true);
        this.updatePriceElement('sol-price', solPrice, false);

        this.updateChangeElement('btc-change', btcChange);
        this.updateChangeElement('eth-change', ethChange);
        this.updateChangeElement('sol-change', solChange);
    }

    updatePriceElement(id, price, isLarge) {
        const element = document.getElementById(id);
        if (element) {
            element.textContent = isLarge
                ? `$${price.toLocaleString('en-US', { maximumFractionDigits: 0 })}`
                : `$${price.toFixed(2)}`;
        }
    }

    updateChangeElement(id, change) {
        const element = document.getElementById(id);
        if (element) {
            const sign = change >= 0 ? '+' : '';
            element.textContent = `${sign}${change.toFixed(2)}%`;
            element.className = `change ${change >= 0 ? 'positive' : 'negative'}`;
        }
    }

    updateFearGreedIndex() {
        const value = Math.floor(55 + Math.random() * 20); // 55-75 range (Greed)
        const fillElement = document.getElementById('fear-greed-fill');
        const valueElement = document.getElementById('fear-greed-value');

        if (fillElement && valueElement) {
            fillElement.style.width = `${value}%`;

            let label = 'Neutral';
            if (value < 25) label = 'Extreme Fear';
            else if (value < 45) label = 'Fear';
            else if (value < 55) label = 'Neutral';
            else if (value < 75) label = 'Greed';
            else label = 'Extreme Greed';

            valueElement.textContent = `${value} - ${label}`;
        }
    }

    formatTime(date) {
        return date.toLocaleTimeString('en-US', {
            hour: 'numeric',
            minute: '2-digit',
            hour12: true
        });
    }

    escapeHTML(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    scrollToBottom() {
        this.chatMessages.scrollTop = this.chatMessages.scrollHeight;
    }

    delay(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }
}

// Initialize the UI
document.addEventListener('DOMContentLoaded', () => {
    new CryptoExpertUI();
});
