"use strict";
const fs=require('node:fs'),path=require('node:path');
const root=path.join(__dirname,'../shiori/static');
const scripts=[...fs.readFileSync(path.join(root,'index.html'),'utf8').matchAll(/<script src="\/static\/([^?"/]+)\?v=/g)].map(match=>match[1]);
const experienceFactories=scripts.filter(name=>/^experience-.+\.js$/.test(name));
// Optional feature controllers have their own focused DOM/protocol regressions.
const integrationScripts=scripts.filter(name=>!['bot-config.js','stickers.js','statistics.js','updates.js','plugins.js'].includes(name));
module.exports={experienceFactories,integrationScripts};
