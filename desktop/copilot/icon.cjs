'use strict';
// Original 32px BGRA bitmap. nativeImage does not decode SVG on Windows.
function trayBitmap(){
 const data=Buffer.alloc(32*32*4);
 for(let y=0;y<32;y++)for(let x=0;x<32;x++){
  const offset=(y*32+x)*4;
  const edge=Math.hypot(Math.max(8-x,0,x-23),Math.max(8-y,0,y-23))<=8;
  const white=Math.abs(x-15.5)+Math.abs(y-15.5)<9;
  data[offset]=white?255:97;data[offset+1]=white?255:115;data[offset+2]=white?255:36;data[offset+3]=edge?255:0;
 }
 return data;
}
module.exports={trayBitmap};
