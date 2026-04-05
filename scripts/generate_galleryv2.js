const sharp = require('sharp');
const fs = require('fs/promises');
const path = require('path');

// --- CANVAS & LAYOUT ---
const WIDTH = 1920;
const HEIGHT = 1200;
const PADDING_LEFT = 150;
const PADDING_TOP = 220; // Top position for the Headline
const SUBLINE_START_Y = 550; // Vertical start for the Sublines (left aligned)
const SUBLINE_LINE_HEIGHT = 55;
const IMAGE_Y_OFFSET = -20; // Extra vertical adjustment for the screenshot

// --- STYLING ---
const FONT_SIZE_HEADLINE = 80;
const FONT_SIZE_SUBLINE = 42;
const CORNER_RADIUS = 24;
const SHADOW_BLUR = 30;
const SHADOW_OFFSET_Y = 25;
const SHADOW_COLOR = 'rgba(0,0,0,0.5)';
const TEXT_COLOR_SECONDARY = '#94A3B8';
const BG_COLOR_START = '#0B0F19';
const BG_COLOR_END = '#1A2235';

const slides = [
    {
        input: '1.png',
        output: '1.png',
        headlineHTML: '<tspan fill="#FFFFFF">Hide Your </tspan><tspan fill="#10B981" font-weight="900">Home IP.</tspan>',
        sublines: [
            'Route your Lightning node securely through',
            'our global VPN servers. Prevent DDoS',
            'attacks &amp; protect your physical location.'
        ]
    },
    {
        input: '2.png',
        output: '2.png',
        headlineHTML: '<tspan fill="#FFFFFF">The Power of </tspan><tspan fill="#10B981" font-weight="900">Hybrid</tspan><tspan fill="#FFFFFF"> Routing.</tspan>',
        sublines: [
            'Combine the anonymity of Tor with the',
            'lightning-fast gossip and settlement',
            'speeds of Clearnet.'
        ]
    },
    {
        input: '3.png',
        output: '3.png',
        headlineHTML: '<tspan fill="#FFFFFF">Total </tspan><tspan fill="#10B981" font-weight="900">Control</tspan><tspan fill="#FFFFFF"> &amp; Visibility.</tspan>',
        sublines: [
            'Monitor your active tunnels, manage your',
            'subscription, and seamlessly toggle your',
            'routing rules—all native to umbrelOS ☂️.'
        ]
    }
];

async function processGallery() {
    for (const slide of slides) {
        console.log(`Processing ${slide.output}...`);

        // Target: tunnelsats/gallery/ relative to project root
        // Since we're in scripts/, we go up one level
        const galleryDir = path.join(__dirname, '..', 'tunnelsats', 'gallery');
        const sourceDir = path.join(galleryDir, 'source');
        
        const inputPath = path.join(sourceDir, slide.input);
        const outputPath = path.join(galleryDir, slide.output);

        // 1. Read and resize screenshot
        const ssTargetSize = 800;
        const rectSvg = Buffer.from(
            `<svg width="${ssTargetSize}" height="${ssTargetSize}"><rect x="0" y="0" width="${ssTargetSize}" height="${ssTargetSize}" rx="${CORNER_RADIUS}" ry="${CORNER_RADIUS}"/></svg>`
        );

        const screenshotBuffer = await fs.readFile(inputPath);
        const roundedScreenshot = await sharp(screenshotBuffer)
            .resize(ssTargetSize, ssTargetSize, { fit: 'cover' })
            .composite([{ input: rectSvg, blend: 'dest-in' }])
            .png()
            .toBuffer();

        // 2. Create a deep drop shadow
        const shadowMargin = SHADOW_BLUR * 2;
        const shadowCanvasSize = ssTargetSize + shadowMargin * 2;
        const shadowSvg = Buffer.from(
            `<svg width="${shadowCanvasSize}" height="${shadowCanvasSize}">
         <filter id="blur">
           <feGaussianBlur stdDeviation="${SHADOW_BLUR}"/>
         </filter>
         <rect x="${shadowMargin}" y="${shadowMargin}" width="${ssTargetSize}" height="${ssTargetSize}" rx="${CORNER_RADIUS}" ry="${CORNER_RADIUS}" fill="${SHADOW_COLOR}" filter="url(#blur)"/>
       </svg>`
        );
        const shadowBuffer = await sharp(shadowSvg).png().toBuffer();

        // 3. Generate the Background and Typography SVG
        const bgSvg = Buffer.from(`
      <svg width="${WIDTH}" height="${HEIGHT}" xmlns="http://www.w3.org/2000/svg">
        <defs>
          <linearGradient id="bgGrad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stop-color="${BG_COLOR_START}" />
            <stop offset="100%" stop-color="${BG_COLOR_END}" />
          </linearGradient>
        </defs>
        <rect width="${WIDTH}" height="${HEIGHT}" fill="url(#bgGrad)" />
        
        <g>
          <text x="${WIDTH / 2}" y="${PADDING_TOP}" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="${FONT_SIZE_HEADLINE}" font-weight="700" letter-spacing="-1.5" text-anchor="middle" xml:space="preserve">
            ${slide.headlineHTML}
          </text>
          
          ${slide.sublines.map((line, idx) => `
            <text x="${PADDING_LEFT}" y="${SUBLINE_START_Y + (idx * SUBLINE_LINE_HEIGHT)}" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif" font-size="${FONT_SIZE_SUBLINE}" font-weight="400" fill="${TEXT_COLOR_SECONDARY}" letter-spacing="-0.5">
              ${line}
            </text>
          `).join('')}
        </g>
      </svg>
    `);

        const imgX = WIDTH - PADDING_LEFT - ssTargetSize;
        const availableHeightBelowHeader = HEIGHT - PADDING_TOP;
        const imgY = PADDING_TOP + (availableHeightBelowHeader / 2) - (ssTargetSize / 2) + IMAGE_Y_OFFSET;

        await sharp(bgSvg)
            .composite([
                {
                    input: shadowBuffer,
                    left: Math.round(imgX - shadowMargin),
                    top: Math.round(imgY - shadowMargin + SHADOW_OFFSET_Y)
                },
                {
                    input: roundedScreenshot,
                    left: Math.round(imgX),
                    top: Math.round(imgY)
                }
            ])
            .toFile(outputPath);
    }

    console.log('Gallery generation complete!');
}

processGallery().catch((err) => {
    console.error(err);
    process.exit(1);
});
