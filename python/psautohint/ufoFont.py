# Copyright 2014 Adobe. All rights reserved.

"""
This module supports using the Adobe FDK tools which operate on 'bez'
files with UFO fonts. It provides low level utilities to manipulate UFO
data without fully parsing and instantiating UFO objects, and without
requiring that the AFDKO contain the robofab libraries.

Modified in Nov 2014, when AFDKO added the robofab libraries. It can now
be used with UFO fonts only to support the hash mechanism.

Developed in order to support checkOutlines and autohint, the code
supports two main functions:
- convert between UFO GLIF and bez formats
- keep a history of processing in a hash map, so that the (lengthy)
processing by autohint and checkOutlines can be avoided if the glyph has
already been processed, and the source data has not changed.

The basic model is:
 - read GLIF file
 - transform GLIF XML element to bez file
 - call FDK tool on bez file
 - transform new bez file to GLIF XML element with new data, and save in list

After all glyphs are done save all the new GLIF XML elements to GLIF
files, and update the hash map.

A complication in the Adobe UFO workflow comes from the fact we want to
make sure that checkOutlines and autohint have been run on each glyph in
a UFO font, when building an OTF font from the UFO font. We need to run
checkOutlines, because we no longer remove overlaps from source UFO font
data, because this can make revising a glyph much easier. We need to run
autohint, because the glyphs must be hinted after checkOutlines is run,
and in any case we want all glyphs to have been autohinted. The problem
with this is that it can take a minute or two to run autohint or
checkOutlines on a 2K-glyph font. The way we avoid this is to make and
keep a hash of the source glyph drawing operators for each tool. When
the tool is run, it calculates a hash of the source glyph, and compares
it to the saved hash. If these are the same, the tool can skip the
glyph. This saves a lot of time: if checkOutlines and autohint are run
on all glyphs in a font, then a second pass is under 2 seconds.

Another issue is that since we no longer remove overlaps from the source
glyph files, checkOutlines must write any edited glyph data to a
different layer in order to not destroy the source data. The ufoFont
defines an Adobe-specific glyph layer for processed glyphs, named
"glyphs.com.adobe.type.processedGlyphs".
checkOutlines writes new glyph files to the processed glyphs layer only
when it makes a change to the glyph data.

When the autohint program is run, the ufoFont must be able to tell
whether checkOutlines has been run and has altered a glyph: if so, the
input file needs to be from the processed glyph layer, else it needs to
be from the default glyph layer.

The way the hashmap works is that we keep an entry for every glyph that
has been processed, identified by a hash of its marking path data. Each
entry contains:
- a hash of the glyph point coordinates, from the default layer.
This is set after a program has been run.
- a history list: a list of the names of each program that has been run,
  in order.
- an editStatus flag.
Altered GLIF data is always written to the Adobe processed glyph layer. The
program may or may not have altered the outline data. For example, autohint
adds private hint data, and adds names to points, but does not change any
paths.

If the stored hash for the glyph does not exist, the ufoFont lib will save the
new hash in the hash map entry and will set the history list to contain just
the current program. The program will read the glyph from the default layer.

If the stored hash matches the hash for the current glyph file in the default
layer, and the current program name is in the history list,then ufoFont
will return "skip=1", and the calling program may skip the glyph.

If the stored hash matches the hash for the current glyph file in the default
layer, and the current program name is not in the history list, then the
ufoFont will return "skip=0". If the font object field 'usedProcessedLayer' is
set True, the program will read the glyph from the from the Adobe processed
layer, if it exists, else it will always read from the default layer.

If the hash differs between the hash map entry and the current glyph in the
default layer, and usedProcessedLayer is False, then ufoFont will return
"skip=0". If usedProcessedLayer is True, then the program will consult the
list of required programs. If any of these are in the history list, then the
program will report an error and return skip =1, else it will return skip=1.
The program will then save the new hash in the hash map entry and reset the
history list to contain just the current program. If the old and new hash
match, but the program name is not in the history list, then the ufoFont will
not skip the glyph, and will add the program name to the history list.


The only tools using this are, at the moment, checkOutlines, checkOutlinesUFO
and autohint. checkOutlines and checkOutlinesUFO use the hash map to skip
processing only when being used to edit glyphs, not when reporting them.
checkOutlines necessarily flattens any components in the source glyph file to
actual outlines. autohint adds point names, and saves the hint data as a
private data in the new GLIF file.

autohint saves the hint data in the GLIF private data area, /lib/dict,
as a key and data pair. See below for the format.

autohint started with _hintFormat1_, a reasonably compact XML representation of
the data. In Sep 2105, autohhint switched to _hintFormat2_ in order to be plist
compatible. This was necessary in order to be compatible with the UFO spec, by
was driven more immediately by the fact the the UFO font file normalization
tools stripped out the _hintFormat1_ hint data as invalid elements.


"""

from __future__ import print_function, absolute_import

import ast
import hashlib
import logging
import os
import plistlib
import re

try:
    import xml.etree.cElementTree as ET
except ImportError:
    import xml.etree.ElementTree as ET

from fontTools.misc.py23 import open, tobytes
from . import fdTools


log = logging.getLogger(__name__)

_hintFormat1_ = """

Deprecated. See _hintFormat2_ below.

A <hintset> element identifies a specific point by its name, and
describes a new set of stem hints which should be applied before the
specific point.

A <flex> element identifies a specific point by its name. The point is
the first point of a curve. The presence of the <flex> element is a
processing suggestion, that the curve and its successor curve should
be converted to a flex operator.

One challenge in applying the hintset and flex elements is that in the
GLIF format, there is no explicit start and end operator: the first path
operator is both the end and the start of the path. I have chosen to
convert this to T1 by taking the first path operator, and making it a
move-to. I then also use it as the last path operator. An exception is a
line-to; in T1, this is omitted, as it is implied by the need to close
the path. Hence, if a hintset references the first operator, there is a
potential ambiguity: should it be applied before the T1 move-to, or
before the final T1 path operator? The logic here applies it before the
move-to only.

 <glyph>
...
    <lib>
        <dict>
            <key><com.adobe.type.autohint><key>
            <data>
                <hintSetList>
                    <hintset pointTag="point name">
                      (<hstem pos="<decimal value>" width="decimal value" />)*
                      (<vstem pos="<decimal value>" width="decimal value" />)*
                      <!-- where n1-5 are decimal values -->
                      <hstem3 stem3List="n0,n1,n2,n3,n4,n5" />*
                      <!-- where n1-5 are decimal values -->
                      <vstem3 stem3List="n0,n1,n2,n3,n4,n5" />*
                    </hintset>*
                    (<hintSetList>*
                        (<hintset pointIndex="positive integer">
                            (<stemindex>positive integer</stemindex>)+
                        </hintset>)+
                    </hintSetList>)*
                    <flexList>
                        <flex pointTag="point Name" />
                    </flexList>*
                </hintSetList>
            </data>
        </dict>
    </lib>
</glyph>

Example from "B" in SourceCodePro-Regular
<key><com.adobe.type.autohint><key>
<data>
<hintSetList id="64bf4987f05ced2a50195f971cd924984047eb1d79c8c43e6a0054f59cc85
dea23a49deb20946a4ea84840534363f7a13cca31a81b1e7e33c832185173369086">
    <hintset pointTag="hintSet0000">
        <hstem pos="0" width="28" />
        <hstem pos="338" width="28" />
        <hstem pos="632" width="28" />
        <vstem pos="100" width="32" />
        <vstem pos="496" width="32" />
    </hintset>
    <hintset pointTag="hintSet0005">
        <hstem pos="0" width="28" />
        <hstem pos="338" width="28" />
        <hstem pos="632" width="28" />
        <vstem pos="100" width="32" />
        <vstem pos="454" width="32" />
        <vstem pos="496" width="32" />
    </hintset>
    <hintset pointTag="hintSet0016">
        <hstem pos="0" width="28" />
        <hstem pos="338" width="28" />
        <hstem pos="632" width="28" />
        <vstem pos="100" width="32" />
        <vstem pos="496" width="32" />
    </hintset>
</hintSetList>
</data>

"""

_hintFormat2_ = """

A <dict> element in the hintSetList array identifies a specific point by its
name, and describes a new set of stem hints which should be applied before the
specific point.

A <string> element in the flexList identifies a specific point by its name.
The point is the first point of a curve. The presence of the element is a
processing suggestion, that the curve and its successor curve should be
converted to a flex operator.

One challenge in applying the hintSetList and flexList elements is that in
the GLIF format, there is no explicit start and end operator: the first path
operator is both the end and the start of the path. I have chosen to convert
this to T1 by taking the first path operator, and making it a move-to. I then
also use it as the last path operator. An exception is a line-to; in T1, this
is omitted, as it is implied by the need to close the path. Hence, if a hintset
references the first operator, there is a potential ambiguity: should it be
applied before the T1 move-to, or before the final T1 path operator? The logic
here applies it before the move-to only.
 <glyph>
...
    <lib>
        <dict>
            <key><com.adobe.type.autohint></key>
            <dict>
                <key>id</key>
                <string> <fingerprint for glyph> </string>
                <key>hintSetList</key>
                <array>
                    <dict>
                      <key>pointTag</key>
                      <string> <point name> </string>
                      <key>stems</key>
                      <array>
                        <string>hstem <position value> <width value></string>*
                        <string>vstem <position value> <width value></string>*
                        <string>hstem3 <position value 0>...<position value 5>
                        </string>*
                        <string>vstem3 <position value 0>...<position value 5>
                        </string>*
                      </array>
                    </dict>*
                </array>

                <key>flexList</key>*
                <array>
                    <string><point name></string>+
                </array>
            </dict>
        </dict>
    </lib>
</glyph>

Example from "B" in SourceCodePro-Regular
<key><com.adobe.type.autohint><key>
<dict>
    <key>id</key>
    <string>64bf4987f05ced2a50195f971cd924984047eb1d79c8c43e6a0054f59cc85dea23
    a49deb20946a4ea84840534363f7a13cca31a81b1e7e33c832185173369086</string>
    <key>hintSetList</key>
    <array>
        <dict>
            <key>pointTag</key>
            <string>hintSet0000</string>
            <key>stems</key>
            <array>
                <string>hstem 338 28</string>
                <string>hstem 632 28</string>
                <string>hstem 100 32</string>
                <string>hstem 496 32</string>
            </array>
        </dict>
        <dict>
            <key>pointTag</key>
            <string>hintSet0005</string>
            <key>stems</key>
            <array>
                <string>hstem 0 28</string>
                <string>hstem 338 28</string>
                <string>hstem 632 28</string>
                <string>hstem 100 32</string>
                <string>hstem 454 32</string>
                <string>hstem 496 32</string>
            </array>
        </dict>
        <dict>
            <key>pointTag</key>
            <string>hintSet0016</string>
            <key>stems</key>
            <array>
                <string>hstem 0 28</string>
                <string>hstem 338 28</string>
                <string>hstem 632 28</string>
                <string>hstem 100 32</string>
                <string>hstem 496 32</string>
            </array>
        </dict>
    </array>
<dict>

"""

XMLElement = ET.Element
xmlToString = ET.tostring


# UFO names
kDefaultGlyphsLayerName = "public.default"
kDefaultGlyphsLayer = "glyphs"
kProcessedGlyphsLayerName = "AFDKO ProcessedGlyphs"
kProcessedGlyphsLayer = "glyphs.com.adobe.type.processedGlyphs"
kFontInfoName = "fontinfo.plist"
kContentsName = "contents.plist"
kLibName = "lib.plist"
kPublicGlyphOrderKey = "public.glyphOrder"

kAdobeDomainPrefix = "com.adobe.type"
kAdobHashMapName = "%s.processedHashMap" % (kAdobeDomainPrefix)
kAdobHashMapVersionName = "hashMapVersion"
kAdobHashMapVersion = (1, 0)  # If major version differs, do not use.
kAutohintName = "autohint"
kCheckOutlineName = "checkOutlines"
kCheckOutlineNameUFO = "checkOutlines"

kOutlinePattern = re.compile(r"<outline.+?outline>", re.DOTALL)

kStemHintsName = "stemhints"
kStemListName = "stemList"
kStemPosName = "pos"
kStemWidthName = "width"
kHStemName = "hstem"
kVStemName = "vstem"
kHStem3Name = "hstem3"
kVStem3Name = "vstem3"
kStem3Pos = "stem3List"
kHintSetListName = "hintSetList"
kFlexListName = "hintSetList"
kHintSetName = "hintset"
kBaseFlexName = "flexCurve"
kPointTag = "pointTag"
kStemIndexName = "stemindex"
kFlexIndexListName = "flexList"
kHintDomainName1 = "com.adobe.type.autohint"
kHintDomainName2 = "com.adobe.type.autohint.v2"
kPointName = "name"
# Hint stuff
kStackLimit = 46
kStemLimit = 96

kDefaultFMNDBPath = "FontMenuNameDB"
kDefaultGOADBPath = "GlyphOrderAndAliasDB"


class UFOParseError(ValueError):
    pass


class BezParseError(ValueError):
    pass


class UFOFontData:
    def __init__(self, parentPath, useHashMap, allow_decimal_coords,
                 write_to_default_layer):
        self.parentPath = parentPath
        self.glyphMap = {}
        self.processedLayerGlyphMap = {}
        self.newGlyphMap = {}
        self.glyphList = []
        self._fontInfo = None
        # if False, will skip reading has map
        # and testing to see if glyph can be skipped.
        self.useHashMap = useHashMap
        # should be used only when calling program is running
        # in report mode only, and not changing any glyph data.
        # Used to skip getting glyph data when glyph hash
        # matches hash of current glyph data.
        self.hashMap = {}
        self.fontDict = None
        self.curSrcDir = None
        self.hashMapChanged = False
        self.glyphDefaultDir = os.path.join(parentPath, "glyphs")
        self.glyphLayerDir = os.path.join(parentPath, kProcessedGlyphsLayer)
        self.glyphWriteDir = self.glyphLayerDir
        self.historyList = []
        # See documentation above.
        self.requiredHistory = []
        # If False, then read data only from the default layer;
        # else read glyphs from processed layer, if it exists.
        self.useProcessedLayer = False
        # If True, then write data to the default layer
        self.writeToDefaultLayer = False
        # if True, then do not skip any glyphs.
        self.doAll = False
        # track whether checkSkipGLyph has deleted an
        # out of date glyph from the processed glyph layer
        self.deletedGlyph = False
        # if true, do NOT round x,y values when processing.
        self.allowDecimalCoords = allow_decimal_coords

        self.loadGlyphMap()

        if write_to_default_layer:
            self.setWriteToDefault()

    def getUnitsPerEm(self):
        return int(self.fontInfo.get("unitsPerEm", 1000))

    def getPSName(self):
        return self.fontInfo.get("postscriptFontName", "PSName-Undefined")

    @staticmethod
    def isCID():
        return 0

    def convertToBez(self, glyphName, removeHints, doAll=False):
        # convertGLIFToBez does not yet support hints;
        # no need for removeHints arg.
        bezString, width = convertGLIFToBez(self, glyphName, doAll)
        return bezString, width

    def updateFromBez(self, bezData, glyphName, width):
        # For UFO font, we don't use the width parameter:
        # it is carried over from the input glif file.
        glifXML = convertBezToGLIF(self, glyphName, bezData)
        self.newGlyphMap[glyphName] = glifXML

    def saveChanges(self):
        if self.hashMapChanged:
            self.writeHashMap()
        self.hashMapChanged = False

        if not os.path.exists(self.glyphWriteDir):
            os.makedirs(self.glyphWriteDir)

        for glyphName, glifXML in self.newGlyphMap.items():
            glyphPath = self.getWriteGlyphPath(glyphName)
            et = ET.ElementTree(glifXML)
            with open(glyphPath, "wb") as fp:
                et.write(fp, encoding="UTF-8", xml_declaration=True)

        # Update the layer contents.plist file
        layerContentsFilePath = os.path.join(self.parentPath,
                                             "layercontents.plist")
        self.updateLayerContents(layerContentsFilePath)
        glyphContentsFilePath = os.path.join(self.glyphWriteDir,
                                             "contents.plist")
        self.updateLayerGlyphContents(glyphContentsFilePath, self.newGlyphMap)

    def getWriteGlyphPath(self, glyphName):
        glyphFileName = self.glyphMap[glyphName]
        if not self.writeToDefaultLayer and \
                glyphName in self.processedLayerGlyphMap:
            glyphFileName = self.processedLayerGlyphMap[glyphName]

        glifFilePath = os.path.join(self.glyphWriteDir, glyphFileName)
        return glifFilePath

    def readHashMap(self):
        hashPath = os.path.join(self.parentPath, "data", kAdobHashMapName)
        if os.path.exists(hashPath):
            with open(hashPath, "rt", encoding="utf-8") as fp:
                data = fp.read()
            newMap = ast.literal_eval(data)
        else:
            newMap = {kAdobHashMapVersionName: kAdobHashMapVersion}

        try:
            version = newMap[kAdobHashMapVersionName]
            if version[0] > kAdobHashMapVersion[0]:
                raise UFOParseError("Hash map version is newer than program. "
                                    "Please update the FDK")
            elif version[0] < kAdobHashMapVersion[0]:
                log.info("Updating hash map: was older version")
                newMap = {kAdobHashMapVersionName: kAdobHashMapVersion}
        except KeyError:
            log.info("Updating hash map: was older version")
            newMap = {kAdobHashMapVersionName: kAdobHashMapVersion}
        self.hashMap = newMap
        return

    def writeHashMap(self):
        hashMap = self.hashMap
        if len(hashMap) == 0:
            return  # no glyphs were processed.

        hashDir = os.path.join(self.parentPath, "data")
        if not os.path.exists(hashDir):
            os.makedirs(hashDir)
        hashPath = os.path.join(hashDir, kAdobHashMapName)

        hasMapKeys = hashMap.keys()
        hasMapKeys = sorted(hasMapKeys)
        data = ["{"]
        for gName in hasMapKeys:
            data.append("'%s': %s," % (gName, hashMap[gName]))
        data.append("}")
        data.append("")
        data = "\n".join(data)
        with open(hashPath, "wb") as fp:
            fp.write(tobytes(data, encoding="utf-8"))

    def getGlyphSrcPath(self, glyphName):
        glyphFileName = self.glyphMap[glyphName]

        # Try for processed layer first.
        if self.useProcessedLayer and self.processedLayerGlyphMap and \
                glyphName in self.processedLayerGlyphMap:
            glyphFileName = self.processedLayerGlyphMap[glyphName]
            self.curSrcDir = self.glyphLayerDir
            glyphPath = os.path.join(self.curSrcDir, glyphFileName)
            if os.path.exists(glyphPath):
                return glyphPath

        self.curSrcDir = self.glyphDefaultDir
        glyphPath = os.path.join(self.curSrcDir, glyphFileName)

        return glyphPath

    def getGlyphDefaultPath(self, glyphName):
        glyphFileName = self.glyphMap[glyphName]
        glyphPath = os.path.join(self.glyphDefaultDir, glyphFileName)
        return glyphPath

    def getGlyphProcessedPath(self, glyphName):
        if self.processedLayerGlyphMap and \
                glyphName in self.processedLayerGlyphMap:
            glyphFileName = self.processedLayerGlyphMap[glyphName]
            return os.path.join(self.glyphLayerDir, glyphFileName)

        return None

    def updateHashEntry(self, glyphName, changed):
        # srcHarsh has already been set: we are fixing the history list.
        if not self.useHashMap:
            return
        # Get hash entry for glyph
        srcHash, historyList = self.hashMap[glyphName]

        self.hashMapChanged = True
        # If the program always reads data from the default layer,
        # and we have just created a new glyph in the processed layer,
        # then reset the history.
        if (not self.useProcessedLayer) and changed:
            self.hashMap[glyphName] = [srcHash, [kAutohintName]]
        else:
            # If the program is not in the history list, add it.
            if kAutohintName not in historyList:
                historyList.append(kAutohintName)

    def checkSkipGlyph(self, glyphName, newSrcHash, doAll):
        skip = False
        if not self.useHashMap:
            return skip

        if len(self.hashMap) == 0:
            # Hash maps have not yet been read in. Get them.
            self.readHashMap()

        hashEntry = srcHash = None
        historyList = []
        programHistoryIndex = -1  # not found in historyList

        # Get hash entry for glyph
        try:
            hashEntry = self.hashMap[glyphName]
            srcHash, historyList = hashEntry
            try:
                programHistoryIndex = historyList.index(kAutohintName)
            except ValueError:
                pass
        except KeyError:
            # Glyph is as yet untouched by any program.
            pass

        if srcHash == newSrcHash:
            if programHistoryIndex >= 0:
                # The glyph has already been processed by this program,
                # and there have been no changes since.
                skip = True and (not doAll)
            if not skip:
                if not self.useProcessedLayer:  # case for Checkoutlines
                    self.hashMapChanged = True
                    self.hashMap[glyphName] = [newSrcHash, [kAutohintName]]
                    glyphPath = self.getGlyphProcessedPath(glyphName)
                    if glyphPath and os.path.exists(glyphPath):
                        os.remove(glyphPath)
                else:
                    if programHistoryIndex < 0:
                        historyList.append(kAutohintName)
        else:
            if self.useProcessedLayer:  # case for autohint
                # default layer glyph and stored glyph hash differ, and
                # useProcessedLayer is True. If any of the programs in
                # requiredHistory in are in the historyList, we need to
                # complain and skip.
                foundMatch = False
                if len(historyList) > 0:
                    for programName in self.requiredHistory:
                        if programName in historyList:
                            foundMatch = True
                if foundMatch:
                    log.error("Glyph '%s' has been edited. You must first "
                              "run '%s' before running '%s'. Skipping.",
                              glyphName, self.requiredHistory,
                              kAutohintName)
                    skip = True

            # If the source hash has changed, we need to delete
            # the processed layer glyph.
            self.hashMapChanged = True
            self.hashMap[glyphName] = [newSrcHash, [kAutohintName]]
            glyphPath = self.getGlyphProcessedPath(glyphName)
            if glyphPath and os.path.exists(glyphPath):
                os.remove(glyphPath)
                self.deletedGlyph = True

        return skip

    @staticmethod
    def getGlyphXML(glyphDir, glyphFileName):
        glyphPath = os.path.join(glyphDir, glyphFileName)  # default
        etRoot = ET.ElementTree()
        glifXML = etRoot.parse(glyphPath)
        outlineXML = glifXML.find("outline")
        try:
            widthXML = glifXML.find("advance")
            if widthXML is not None:
                width = int(widthXML.get("width"))
            else:
                width = 1000
        except UFOParseError:
            log.exception("Skipping glyph '%s' because of parse error.",
                          glyphFileName)
            return None, None, None
        return width, glifXML, outlineXML

    def getOrSkipGlyphXML(self, glyphName, doAll=False):
        # Get default glyph layer data, so we can check if the glyph
        # has been edited since this program was last run.
        # If the program name is in the history list, and the srcHash
        # matches the default glyph layer data, we can skip.
        glyphFileName = self.glyphMap[glyphName]
        width, glifXML, outlineXML = self.getGlyphXML(
            self.glyphDefaultDir, glyphFileName)
        if glifXML is None:
            skip = True
            return None, None, skip

        # Hash is always from the default glyph layer.
        useDefaultGlyphDir = True
        newHash, _ = self.buildGlyphHashValue(
            width, outlineXML, glyphName, useDefaultGlyphDir)
        skip = self.checkSkipGlyph(glyphName, newHash, doAll)

        # If self.useProcessedLayer and there is a glyph
        # in the processed layer, get the outline from that.
        if self.useProcessedLayer and self.processedLayerGlyphMap:
            if glyphName in self.processedLayerGlyphMap:
                glyphFileName = self.processedLayerGlyphMap[glyphName]
            glyphPath = os.path.join(self.glyphLayerDir, glyphFileName)
            if os.path.exists(glyphPath):
                width, glifXML, outlineXML = self.getGlyphXML(
                    self.glyphLayerDir, glyphFileName)
                if glifXML is None:
                    skip = True
                    return None, None, skip

        return width, outlineXML, skip

    def getGlyphList(self):
        return self.glyphList

    def loadGlyphMap(self):
        # Need to both get the list of glyphs from contents.plist, and also
        # the glyph order. The latter is take from the public.glyphOrder key
        # in lib.py, if it exists, else it is taken from the contents.plist
        # file. Any glyphs in contents.plist which are not named in the
        # public.glyphOrder are sorted after all glyphs which are named
        # in the public.glyphOrder,, in the order that they occured in
        # contents.plist.
        contentsPath = os.path.join(self.parentPath, "glyphs", kContentsName)
        self.glyphMap, self.glyphList = parsePList(contentsPath)
        orderPath = os.path.join(self.parentPath, kLibName)
        self.orderMap = parseGlyphOrder(orderPath)
        if self.orderMap is not None:
            orderIndex = len(self.orderMap)
            orderList = []

            # If there are glyphs in the font which are not named in the
            # public.glyphOrder entry, add then in the order of the
            # contents.plist file.
            for glyphName in self.glyphList:
                try:
                    entry = [self.orderMap[glyphName], glyphName]
                except KeyError:
                    entry = [orderIndex, glyphName]
                    self.orderMap[glyphName] = orderIndex
                    orderIndex += 1
                orderList.append(entry)
            self.glyphList = [entry[1] for entry in sorted(orderList)]
        else:
            self.orderMap = {}
            numGlyphs = len(self.glyphList)
            for i in range(numGlyphs):
                self.orderMap[self.glyphList[i]] = i

        # I also need to get the glyph map for the processed layer,
        # and use this when the glyph is read from the processed layer.
        # glyph file names that differ from what is in the default glyph layer.
        # Because checkOutliensUFO used the defcon library, it can write
        contentsPath = os.path.join(self.glyphLayerDir, kContentsName)
        if os.path.exists(contentsPath):
            self.processedLayerGlyphMap, self.processedLayerGlyphList = \
                parsePList(contentsPath)

    @property
    def fontInfo(self):
        if self._fontInfo is None:
            fontInfoPath = os.path.join(self.parentPath, "fontinfo.plist")
            if os.path.exists(fontInfoPath):
                self._fontInfo, _ = parsePList(fontInfoPath)
            else:
                self._fontInfo = {}
        return self._fontInfo

    @staticmethod
    def updateLayerContents(contentsFilePath):
        if os.path.exists(contentsFilePath):
            contentsList = plistlib.readPlist(contentsFilePath)
            # If the layer name already exists, don't add a new one,
            # or change the path
            seenPublicDefault = False
            seenProcessedGlyph = False
            for _, layerPath in contentsList:
                if layerPath == kProcessedGlyphsLayer:
                    seenProcessedGlyph = True
                if layerPath == kDefaultGlyphsLayer:
                    seenPublicDefault = True
            update = False
            if not seenPublicDefault:
                update = True
                contentsList = [[kDefaultGlyphsLayerName,
                                kDefaultGlyphsLayer]] + contentsList
            if not seenProcessedGlyph:
                update = True
                contentsList.append([kProcessedGlyphsLayerName,
                                     kProcessedGlyphsLayer])
            if update:
                plistlib.writePlist(contentsList, contentsFilePath)
        else:
            contentsList = [[kDefaultGlyphsLayerName, kDefaultGlyphsLayer]]
            contentsList.append([kProcessedGlyphsLayerName,
                                 kProcessedGlyphsLayer])
        plistlib.writePlist(contentsList, contentsFilePath)

    def updateLayerGlyphContents(self, contentsFilePath, newGlyphData):
        if os.path.exists(contentsFilePath):
            contentsDict = plistlib.readPlist(contentsFilePath)
        else:
            contentsDict = {}
        for glyphName in newGlyphData.keys():
            # Try for processed layer first.
            if self.useProcessedLayer and self.processedLayerGlyphMap:
                if glyphName in self.processedLayerGlyphMap:
                    contentsDict[glyphName] = \
                        self.processedLayerGlyphMap[glyphName]
                else:
                    contentsDict[glyphName] = self.glyphMap[glyphName]
            else:
                contentsDict[glyphName] = self.glyphMap[glyphName]
        plistlib.writePlist(contentsDict, contentsFilePath)

    def getFontInfo(self, fontPSName, inputPath, allow_no_blues, noFlex,
                    vCounterGlyphs, hCounterGlyphs, fdIndex=0):
        if self.fontDict is not None:
            return self.fontDict

        fdDict = fdTools.FDDict()
        # should be 1 if the glyphs are ideographic, else 0.
        fdDict.LanguageGroup = self.fontInfo.get("languagegroup", "0")
        fdDict.OrigEmSqUnits = self.getUnitsPerEm()
        fdDict.FontName = self.getPSName()
        upm = self.getUnitsPerEm()
        low = min(-upm * 0.25, float(
            self.fontInfo.get("openTypeOS2WinDescent", "0")) - 200)
        high = max(upm * 1.25, float(
            self.fontInfo.get("openTypeOS2WinAscent", "0")) + 200)
        # Make a set of inactive alignment zones: zones outside of the font
        # bbox so as not to affect hinting. Used when src font has no
        # BlueValues or has invalid BlueValues. Some fonts have bad BBox
        # values, so I don't let this be smaller than -upm*0.25, upm*1.25.
        inactiveAlignmentValues = [low, low, high, high]
        blueValues = self.fontInfo.get("postscriptBlueValues", [])
        numBlueValues = len(blueValues)
        if numBlueValues < 4:
            if allow_no_blues:
                blueValues = inactiveAlignmentValues
                numBlueValues = len(blueValues)
            else:
                raise UFOParseError(
                    "Font must have at least four values in its "
                    "BlueValues array for PSAutoHint to work!")
        blueValues.sort()
        # The first pair only is a bottom zone, where the first value is the
        # overshoot position; the rest are top zones, and second value of the
        # pair is the overshoot position.
        blueValues[0] = blueValues[0] - blueValues[1]
        for i in range(3, numBlueValues, 2):
            blueValues[i] = blueValues[i] - blueValues[i - 1]

        blueValues = [str(v) for v in blueValues]
        numBlueValues = min(numBlueValues, len(fdTools.kBlueValueKeys))
        for i in range(numBlueValues):
            key = fdTools.kBlueValueKeys[i]
            value = blueValues[i]
            setattr(fdDict, key, value)

        otherBlues = self.fontInfo.get("postscriptOtherBlues", [])
        numBlueValues = len(otherBlues)

        if len(otherBlues) > 0:
            i = 0
            numBlueValues = len(otherBlues)
            otherBlues.sort()
            for i in range(0, numBlueValues, 2):
                otherBlues[i] = otherBlues[i] - otherBlues[i + 1]
            otherBlues = [str(v) for v in otherBlues]
            numBlueValues = min(numBlueValues,
                                len(fdTools.kOtherBlueValueKeys))
            for i in range(numBlueValues):
                key = fdTools.kOtherBlueValueKeys[i]
                value = otherBlues[i]
                setattr(fdDict, key, value)

        vstems = self.fontInfo.get("postscriptStemSnapV", [])
        if len(vstems) == 0:
            if allow_no_blues:
                # dummy value. Needs to be larger than any hint will likely be,
                # as the autohint program strips out any hint wider than twice
                # the largest global stem width.
                vstems = [fdDict.OrigEmSqUnits]
            else:
                raise UFOParseError("Font does not have postscriptStemSnapV!")

        vstems.sort()
        if (len(vstems) == 0) or ((len(vstems) == 1) and (vstems[0] < 1)):
            # dummy value that will allow PyAC to run
            vstems = [fdDict.OrigEmSqUnits]
            log.warning("There is no value or 0 value for DominantV.")
        fdDict.DominantV = "[" + " ".join([str(v) for v in vstems]) + "]"

        hstems = self.fontInfo.get("postscriptStemSnapH", [])
        if len(hstems) == 0:
            if allow_no_blues:
                # dummy value. Needs to be larger than any hint will likely be,
                # as the autohint program strips out any hint wider than twice
                # the largest global stem width.
                hstems = [fdDict.OrigEmSqUnits]
            else:
                raise UFOParseError("Font does not have postscriptStemSnapH!")

        hstems.sort()
        if (len(hstems) == 0) or ((len(hstems) == 1) and (hstems[0] < 1)):
            # dummy value that will allow PyAC to run
            hstems = [fdDict.OrigEmSqUnits]
            log.warning("There is no value or 0 value for DominantH.")
        fdDict.DominantH = "[" + " ".join([str(v) for v in hstems]) + "]"

        if noFlex:
            fdDict.FlexOK = "false"
        else:
            fdDict.FlexOK = "true"

        # Add candidate lists for counter hints, if any.
        if vCounterGlyphs:
            temp = " ".join(vCounterGlyphs)
            fdDict.VCounterChars = "( %s )" % (temp)
        if hCounterGlyphs:
            temp = " ".join(hCounterGlyphs)
            fdDict.HCounterChars = "( %s )" % (temp)

        fdDict.BlueFuzz = self.fontInfo.get("postscriptBlueFuzz", 1)
        # postscriptBlueShift
        # postscriptBlueScale
        self.fontDict = fdDict
        return fdDict

    @staticmethod
    def getfdIndex(gid):
        return 0

    def getfdInfo(self, psName, inputPath, allow_no_blues, noFlex,
                  vCounterGlyphs, hCounterGlyphs, glyphList, fdIndex=0):
        fontDictList = []
        fdGlyphDict = None
        fdDict = self.getFontInfo(psName, inputPath, allow_no_blues, noFlex,
                                  vCounterGlyphs, hCounterGlyphs, fdIndex)
        fontDictList.append(fdDict)

        # Check the fontinfo file, and add any other font dicts
        srcFontInfo = os.path.dirname(inputPath)
        srcFontInfo = os.path.join(srcFontInfo, "fontinfo")
        maxX = self.getUnitsPerEm() * 2
        maxY = maxX
        minY = -self.getUnitsPerEm()
        if os.path.exists(srcFontInfo):
            with open(srcFontInfo, "r", encoding="utf-8") as fi:
                fontInfoData = fi.read()
            fontInfoData = re.sub(r"#[^\r\n]+", "", fontInfoData)

            if "FDDict" in fontInfoData:

                blueFuzz = fdDict.BlueFuzz
                fdGlyphDict, fontDictList, finalFDict = \
                    fdTools.parseFontInfoFile(
                        fontDictList, fontInfoData, glyphList, maxY, minY,
                        psName, blueFuzz)
                if finalFDict is None:
                    # If a font dict was not explicitly specified for the
                    # output font, use the first user-specified font dict.
                    fdTools.mergeFDDicts(fontDictList[1:], self.fontDict)
                else:
                    fdTools.mergeFDDicts([finalFDict], self.fontDict)

        return fdGlyphDict, fontDictList

    def getGlyphID(self, glyphName):
        try:
            gid = self.orderMap[glyphName]
        except IndexError:
            raise UFOParseError(
                "Could not find glyph name '%s' in UFO font contents plist. "
                "'%s'. " % (glyphName, self.parentPath))
        return gid

    def buildGlyphHashValue(self, width, outlineXML, glyphName,
                            useDefaultGlyphDir, level=0):
        """
        glyphData must be the official <outline> XML from a GLIF.
        We skip contours with only one point.
        """
        dataList = ["w%s" % (str(width))]
        if level > 10:
            raise UFOParseError(
                "In parsing component, exceeded 10 levels of reference. '%s'."
                % (glyphName))
        # <outline> tag is optional per spec., e.g. space glyph
        # does not necessarily have it.
        if outlineXML:
            for childContour in outlineXML:
                if childContour.tag == "contour":
                    if len(childContour) < 2:
                        continue
                    else:
                        for child in childContour:
                            if child.tag == "point":
                                try:
                                    pointType = child.attrib["type"][0]
                                except KeyError:
                                    pointType = ""
                                dataList.append("%s%s%s" % (
                                    pointType, child.attrib["x"],
                                    child.attrib["y"]))
                elif childContour.tag == "component":
                    # append the component hash.
                    try:
                        compGlyphName = childContour.attrib["base"]
                        dataList.append("%s%s" % ("base:", compGlyphName))
                    except KeyError:
                        raise UFOParseError(
                            "'%s' is missing the 'base' attribute in a "
                            "component. glyph '%s'." % glyphName)

                    if useDefaultGlyphDir:
                        try:
                            componentPath = self.getGlyphDefaultPath(
                                compGlyphName)
                        except KeyError:
                            raise UFOParseError(
                                "'%s' component glyph is missing from "
                                "contents.plist." % compGlyphName)
                    else:
                        # If we are not necessarily using the default layer for
                        # the main glyph, then a missing component may not have
                        # been processed, and may just be in the default layer.
                        # We need to look for component glyphs in the src list
                        # first, then in the defualt layer.
                        try:
                            componentPath = self.getGlyphSrcPath(compGlyphName)
                            if not os.path.exists(componentPath):
                                componentPath = self.getGlyphDefaultPath(
                                    compGlyphName)
                        except KeyError:
                            try:
                                componentPath = self.getGlyphDefaultPath(
                                    compGlyphName)
                            except KeyError:
                                raise UFOParseError(
                                    "'%s' component glyph is missing from "
                                    "contents.plist." % compGlyphName)

                    if not os.path.exists(componentPath):
                        raise UFOParseError(
                            "'%s' component file is missing: '%s'." %
                            (compGlyphName, componentPath))

                    etRoot = ET.ElementTree()

                    # Collect transformm fields, if any.
                    for transformTag in ["xScale", "xyScale", "yxScale",
                                         "yScale", "xOffset", "yOffset"]:
                        try:
                            value = childContour.attrib[transformTag]
                            if self._represents_integer(value):
                                value = str(int(value))
                            dataList.append(value)
                        except KeyError:
                            pass
                    componentXML = etRoot.parse(componentPath)
                    componentOutlineXML = componentXML.find("outline")
                    _, componentDataList = self.buildGlyphHashValue(
                        width, componentOutlineXML, glyphName,
                        useDefaultGlyphDir, level + 1)
                    dataList.extend(componentDataList)
        data = "".join(dataList)
        if len(data) < 128:
            glyph_hash = data
        else:
            glyph_hash = hashlib.sha512(data.encode("utf-8")).hexdigest()
        return glyph_hash, dataList

    @staticmethod
    def _represents_integer(string):
        try:
            int(string)
            return True
        except ValueError:
            return False

    def getComponentOutline(self, componentItem):
        try:
            compGlyphName = componentItem.attrib["base"]
        except KeyError:
            raise UFOParseError(
                "'%s' attribute missing from component '%s'." %
                ("base", xmlToString(componentItem)))

        if not self.useProcessedLayer:
            try:
                compGlyphFilePath = self.getGlyphDefaultPath(compGlyphName)
            except KeyError:
                raise UFOParseError(
                    "'%s' component glyph is missing from contents.plist." %
                    compGlyphName)
        else:
            # If we are not necessarily using the default layer for the main
            # glyph, then a missing component may not have been processed, and
            # may just be in the default layer. We need to look for component
            # glyphs in the src list first, then in the defualt layer.
            try:
                compGlyphFilePath = self.getGlyphSrcPath(compGlyphName)
                if not os.path.exists(compGlyphFilePath):
                    compGlyphFilePath = self.getGlyphDefaultPath(compGlyphName)
            except KeyError:
                try:
                    compGlyphFilePath = self.getGlyphDefaultPath(compGlyphName)
                except KeyError:
                    raise UFOParseError(
                        "'%s' component glyph is missing from contents.plist."
                        % compGlyphName)

        if not os.path.exists(compGlyphFilePath):
            raise UFOParseError(
                "'%s' component file is missing: '%s'." %
                (compGlyphName, compGlyphFilePath))

        etRoot = ET.ElementTree()
        glifXML = etRoot.parse(compGlyphFilePath)
        outlineXML = glifXML.find("outline")
        return outlineXML

    @staticmethod
    def copyTo(dstPath):
        """ Copy UFO font to target path"""
        return

    def close(self):
        if self.hashMapChanged:
            self.writeHashMap()
            self.hashMapChanged = False
        return

    def setWriteToDefault(self):
        self.useProcessedLayer = False
        self.writeToDefaultLayer = True
        self.glyphWriteDir = self.glyphDefaultDir


def parseGlyphOrder(filePath):
    orderMap = None
    if os.path.exists(filePath):
        publicOrderDict, _ = parsePList(filePath, kPublicGlyphOrderKey)
        if publicOrderDict is not None:
            orderMap = {}
            glyphList = publicOrderDict[kPublicGlyphOrderKey]
            numGlyphs = len(glyphList)
            for i in range(numGlyphs):
                orderMap[glyphList[i]] = i
    return orderMap


def parsePList(filePath, dictKey=None):
    # if dictKey is defined, parse and return only the data for that key.
    # This helps avoid needing to parse all plist types. I need to support
    # only data for known keys amd lists.
    plistDict = {}
    plistKeys = []

    # I uses this rather than the plistlib in order
    # to get a list that allows preserving key order.
    contents = ET.parse(filePath).getroot()
    ufo_dict = contents.find("dict")
    if ufo_dict is None:
        raise UFOParseError("In '%s', failed to find dict. '%s'." % (filePath))
    lastTag = "string"
    for child in ufo_dict:
        if child.tag == "key":
            if lastTag == "key":
                raise UFOParseError(
                    "In contents.plist, key name '%s' followed another key "
                    "name '%s'." % (xmlToString(child), lastTag))
            skipKeyData = False
            lastName = child.text
            lastTag = "key"
            if dictKey is not None:
                if lastName != dictKey:
                    skipKeyData = True
        elif child.tag != "key":
            if lastTag != "key":
                raise UFOParseError(
                    "In contents.plist, key value '%s' followed a non-key "
                    "tag '%s'." % (xmlToString(child), lastTag))
            lastTag = child.tag

            if skipKeyData:
                continue

            if lastName in plistDict:
                raise UFOParseError(
                    "Encountered duplicate key name '%s' in '%s'." %
                    (lastName, filePath))
            if child.tag == "array":
                plistDict[lastName] = []
                for listChild in child:
                    val = listChild.text
                    if listChild.tag == "integer":
                        val = int(val)
                    elif listChild.tag == "real":
                        val = float(val)
                    elif listChild.tag == "string":
                        pass
                    else:
                        raise UFOParseError(
                            "In plist file, encountered unhandled key type "
                            "'%s' in '%s' for parent key %s. %s." %
                            (listChild.tag, child.tag, lastName, filePath))
                    plistDict[lastName].append(val)
            elif child.tag == "integer":
                plistDict[lastName] = int(child.text)
            elif child.tag == "real":
                plistDict[lastName] = float(child.text)
            elif child.tag == "false":
                plistDict[lastName] = 0
            elif child.tag == "true":
                plistDict[lastName] = 1
            elif child.tag == "string":
                plistDict[lastName] = child.text
            else:
                raise UFOParseError(
                    "In plist file, encountered unhandled key type '%s' for "
                    "key %s. %s." % (child.tag, lastName, filePath))
            plistKeys.append(lastName)
        else:
            raise UFOParseError(
                "In plist file, encountered unexpected element '%s'. %s." %
                (xmlToString(child), filePath))
    if len(plistDict) == 0:
        plistDict = None
    return plistDict, plistKeys


class UFOTransform:
    kTransformTagList = ["xScale", "xyScale", "yxScale", "yScale",
                         "xOffset", "yOffset"]

    def __init__(self, componentXML):
        self.transformFactors = []
        self.isDefault = True
        self.isOffsetOnly = True
        hasScale = False
        hasOffset = False
        for tag in self.kTransformTagList:
            val = componentXML.attrib.get(tag, None)
            if tag in ["xScale", "yScale"]:
                if val is None:
                    val = 1.0
                else:
                    val = float(val)
                    if val != 1.0:
                        hasScale = True
            else:
                if val is None:
                    val = 0
                else:
                    val = float(val)
                    if val != 0:
                        self.isDefault = False
                        if tag in ["xyScale", "yxScale"]:
                            hasScale = True
                        elif tag in ["xOffset", "yOffset"]:
                            hasOffset = True
                        else:
                            raise UFOParseError(
                                "Unknown tag '%s' in component '%s'." %
                                (tag, xmlToString(componentXML)))

            self.transformFactors.append(val)
        if hasScale or hasOffset:
            self.isDefault = False
        if hasScale:
            self.isOffsetOnly = False

    def concat(self, transform):
        if transform is None:
            return
        if transform.isDefault:
            return
        tfCur = self.transformFactors
        tfPrev = transform.transformFactors
        if transform.isOffsetOnly:
            tfCur[4] += tfPrev[4]
            tfCur[5] += tfPrev[5]
            self.isDefault = False
        else:
            tfNew = [0.0] * 6
            tfNew[0] = tfCur[0] * tfPrev[0] + tfCur[1] * tfPrev[2]
            tfNew[1] = tfCur[0] * tfPrev[1] + tfCur[1] * tfPrev[3]
            tfNew[2] = tfCur[2] * tfPrev[0] + tfCur[3] * tfPrev[2]
            tfNew[3] = tfCur[2] * tfPrev[1] + tfCur[3] * tfPrev[3]
            tfNew[4] = tfCur[4] * tfPrev[0] + tfCur[5] * tfPrev[2] + tfPrev[4]
            tfNew[5] = tfCur[4] * tfPrev[1] + tfCur[5] * tfPrev[3] + tfPrev[5]
            self.transformFactors = tfNew
            self.isOffsetOnly = self.isOffsetOnly and transform.isOffsetOnly
            self.isDefault = False

    def apply(self, x, y):
        tfCur = self.transformFactors
        if self.isOffsetOnly:
            x += tfCur[4]
            y += tfCur[5]
        else:
            x, y = (x * tfCur[0] + y * tfCur[2] + tfCur[4],
                    x * tfCur[1] + y * tfCur[3] + tfCur[5])
        return x, y


def convertGlyphOutlineToBezString(outlineXML, ufoFontData, transform=None,
                                   level=0):
    """
    Convert XML outline element containing contours and components
    to a bez string.
    Since xml.etree.CElementTree is compiled code, this
    will run faster than tokenizing and parsing in regular Python.

    glyphMap is a dict mapping glyph names to component file names. If it
    has a length of 1, it needs to be filled from the contents.plist file.
    It must have a key/value [UFO_FONTPATH] = path to parent UFO font.

    transformList is None, or a list of floats in the order:
    ["xScale", "xyScale", "yxScale", "yScale", "xOffset", "yOffset"]

    Build a list of outlines and components.
    For each item:
        if is outline:
            get list of points
            for each point:
                if transform matrix:
                    apply transform to coordinate
                if off line
                    push coord on stack
                if online:
                    add operator
            if is component:
                if any scale, skew, or offset factors:
                    set transform
                    call convertGlyphOutlineToBezString with transform
                    add to bez string
    I don't bother adding in any hinting info, as this is used only for making
    temp bez files as input to checkOutlines or autohint, which invalidate
    hinting data by changing the outlines data, or at best ignore hinting data.

    bez ops output: ["rrmt", "dt", "rct", "cp" , "ed"]
    """

    if level > 10:
        raise UFOParseError(
            "In parsing component, exceeded 10 levels of reference. '%s'. " %
            outlineXML)

    allowDecimals = ufoFontData.allowDecimalCoords

    bezStringList = []
    if outlineXML is None:
        bezstring = ""
    else:
        for outlineItem in outlineXML:

            if outlineItem.tag == "component":
                newTransform = UFOTransform(outlineItem)
                if newTransform.isDefault:
                    if transform is not None:
                        newTransform.concat(transform)
                    else:
                        newTransform = None
                else:
                    if transform is not None:
                        newTransform.concat(transform)
                componentOutline = ufoFontData.getComponentOutline(outlineItem)
                if componentOutline:
                    outlineBezString = convertGlyphOutlineToBezString(
                        componentOutline, ufoFontData, newTransform, level + 1)
                    bezStringList.append(outlineBezString)
                continue

            if outlineItem.tag != "contour":
                continue

            # May be an empty contour.
            if len(outlineItem) == 0:
                continue

            # We have a regular contour. Iterate over points.
            argStack = []
            # Deal with setting up move-to.
            lastItem = outlineItem[0]
            try:
                point_type = lastItem.attrib["type"]
            except KeyError:
                point_type = "offcurve"
            if point_type in ["curve", "line", "qccurve"]:
                outlineItem = outlineItem[1:]
                if point_type != "line":
                    # I don't do this for line, as psautohint behaves
                    # differently when a final line-to is explicit.
                    outlineItem.append(lastItem)
                x = float(lastItem.attrib["x"])
                y = float(lastItem.attrib["y"])
                if transform is not None:
                    x, y = transform.apply(x, y)

                if not allowDecimals:
                    x = int(round(x))
                    y = int(round(y))

                if allowDecimals:
                    op = "%.3f %.3f mt" % (x, y)
                else:
                    op = "%s %s mt" % (x, y)
                bezStringList.append(op)
            elif point_type == "move":
                # first op is a move-to.
                if len(outlineItem) == 1:
                    # this is a move, and is the only op in this outline.
                    # Don't pass it through. This is most likely left over
                    # from a GLIF format 1 anchor.
                    argStack = []
                    continue
            elif point_type == "offcurve":
                # We should only see an off curve point as the first point
                # when the first op is a curve and the last op is a line.
                # In this case, insert a move-to to the line's coords, then
                # omit the line.
                # Breaking news! In rare cases, a first off-curve point can
                # occur when the first AND last op is a curve.
                curLastItem = outlineItem[-1]
                # In this case, insert a move-to to the last op's end pos.
                try:
                    lastType = curLastItem.attrib["type"]
                    x = float(curLastItem.attrib["x"])
                    y = float(curLastItem.attrib["y"])
                    if transform is not None:
                        x, y = transform.apply(x, y)

                    if not allowDecimals:
                        x = int(round(x))
                        y = int(round(y))

                    if lastType == "line":

                        if allowDecimals:
                            op = "%.3f %.3f mt" % (x, y)
                        else:
                            op = "%s %s mt" % (x, y)

                        bezStringList.append(op)
                        outlineItem = outlineItem[:-1]
                    elif lastType == "curve":

                        if allowDecimals:
                            op = "%.3f %.3f mt" % (x, y)
                        else:
                            op = "%s %s mt" % (x, y)
                        bezStringList.append(op)

                except KeyError:
                    raise UFOParseError(
                        "Unhandled case for first and last points in outline "
                        "'%s'." % xmlToString(outlineItem))
            else:
                raise UFOParseError(
                    "Unhandled case for first point in outline '%s'." %
                    xmlToString(outlineItem))

            for contourItem in outlineItem:
                if contourItem.tag != "point":
                    continue
                x = float(contourItem.attrib["x"])
                y = float(contourItem.attrib["y"])
                if transform is not None:
                    x, y = transform.apply(x, y)

                if not allowDecimals:
                    x = int(round(x))
                    y = int(round(y))

                try:
                    point_type = contourItem.attrib["type"]
                except KeyError:
                    point_type = "offcurve"

                if point_type == "offcurve":
                    argStack.append(x)
                    argStack.append(y)
                elif point_type == "move":
                    if allowDecimals:
                        op = "%.3f %.3f mt" % (x, y)
                    else:
                        op = "%s %s mt" % (x, y)
                    bezStringList.append(op)
                    argStack = []
                elif point_type == "line":
                    if allowDecimals:
                        op = "%.3f %.3f dt" % (x, y)
                    else:
                        op = "%s %s dt" % (x, y)
                    bezStringList.append(op)
                    argStack = []
                elif point_type == "curve":
                    if len(argStack) != 4:
                        raise UFOParseError(
                            "Argument stack error seen for curve point '%s'." %
                            xmlToString(contourItem))
                    if allowDecimals:
                        op = "%.3f %.3f %.3f %.3f %.3f %.3f ct" % \
                             (argStack[0], argStack[1], argStack[2],
                              argStack[3], x, y)
                    else:
                        op = "%s %s %s %s %s %s ct" % \
                             (argStack[0], argStack[1], argStack[2],
                              argStack[3], x, y)
                    argStack = []
                    bezStringList.append(op)
                elif point_type == "qccurve":
                    raise UFOParseError(
                        "Point type not supported '%s'." %
                        xmlToString(contourItem))
                else:
                    raise UFOParseError(
                        "Unknown Point type not supported '%s'." %
                        xmlToString(contourItem))

            # we get here only if there was at least a move.
            bezStringList.append("cp")
        bezstring = "\n".join(bezStringList)
    return bezstring


def convertGLIFToBez(ufoFontData, glyphName, doAll=False):
    width, outlineXML, skip = ufoFontData.getOrSkipGlyphXML(glyphName, doAll)
    if skip:
        return None, width

    if outlineXML is None:
        return None, width

    bezString = convertGlyphOutlineToBezString(outlineXML, ufoFontData)
    bezString = "\n".join(["% " + glyphName, "sc", bezString, "ed", ""])
    return bezString, width


class HintMask:
    # class used to collect hints for the current
    # hint mask when converting bez to T2.
    def __init__(self, listPos):
        # The index into the pointList is kept
        # so we can quickly find them later.
        self.listPos = listPos
        self.hList = []  # These contain the actual hint values.
        self.vList = []
        self.hstem3List = []
        self.vstem3List = []
        # The name attribute of the point which follows the new hint set.
        self.pointName = "hintSet" + str(listPos).zfill(4)

    def addHintSet(self, hintSetList):
        # Add the hint set to hintSetList
        newHintSetDict = XMLElement("dict")
        hintSetList.append(newHintSetDict)

        newHintSetKey = XMLElement("key")
        newHintSetKey.text = kPointTag
        newHintSetDict.append(newHintSetKey)

        newHintSetValue = XMLElement("string")
        newHintSetValue.text = self.pointName
        newHintSetDict.append(newHintSetValue)

        stemKey = XMLElement("key")
        stemKey.text = "stems"
        newHintSetDict.append(stemKey)

        newHintSetArray = XMLElement("array")
        newHintSetDict.append(newHintSetArray)

        if (len(self.hList) > 0) or (len(self.vstem3List)):
            isH = True
            addHintList(self.hList, self.hstem3List, newHintSetArray, isH)

        if (len(self.vList) > 0) or (len(self.vstem3List)):
            isH = False
            addHintList(self.vList, self.vstem3List, newHintSetArray, isH)


def makeStemHintList(hintsStem3, stemList, isH):
    # In bez terms, the first coordinate in each pair is
    # absolute, second is relative, and hence is the width.
    if isH:
        op = kHStem3Name
    else:
        op = kVStem3Name
    newStem = XMLElement("string")
    posList = [op]
    for stem3 in hintsStem3:
        for pos, width in stem3:
            if (isinstance(pos, float)) and (int(pos) == pos):
                pos = int(pos)
            if (isinstance(width, float)) and (int(width) == width):
                width = int(width)
            posList.append("%s %s" % (pos, width))
    posString = " ".join(posList)
    newStem.text = posString
    stemList.append(newStem)


def makeHintList(hints, newHintSetArray, isH):
    # Add the list of hint operators
    # In bez terms, the first coordinate in each pair is
    # absolute, second is relative, and hence is the width.
    for hint in hints:
        if not hint:
            continue
        pos = hint[0]
        if (isinstance(pos, float)) and (int(pos) == pos):
            pos = int(pos)
        width = hint[1]
        if (isinstance(width, float)) and (int(width) == width):
            width = int(width)
        if isH:
            op = kHStemName
        else:
            op = kVStemName
        newStem = XMLElement("string")
        newStem.text = "%s %s %s" % (op, pos, width)
        newHintSetArray.append(newStem)


def addFlexHint(flexList, flexArray):
    for pointTag in flexList:
        newFlexTag = XMLElement("string")
        newFlexTag.text = pointTag
        flexArray.append(newFlexTag)


def fixStartPoint(outlineItem, opList):
    # For the GLIF format, the idea of first/last point is funky, because
    # the format avoids identifying a start point. This means there is no
    # implied close-path line-to. If the last implied or explicit path-close
    # operator is a line-to, then replace the "mt" with linto, and remove
    # the last explicit path-closing line-to, if any. If the last op is a
    # curve, then leave the first two point args on the stack at the end of
    # the point list, and move the last curveto to the first op, replacing
    # the move-to.

    _, firstX, firstY = opList[0]
    lastOp, lastX, lastY = opList[-1]
    firstPointElement = outlineItem[0]
    if (firstX == lastX) and (firstY == lastY):
        del outlineItem[-1]
        firstPointElement.set("type", lastOp)
    else:
        # we have an implied final line to. All we need to do
        # is convert the inital moveto to a lineto.
        firstPointElement.set("type", "line")


bezToUFOPoint = {
    "mt": 'move',
    "rmt": 'move',
    "dt": 'line',
    "ct": 'curve',
}


def convertCoords(curX, curY):
    showX = int(curX)
    if showX != curX:
        showX = curX
    showY = int(curY)
    if showY != curY:
        showY = curY
    return showX, showY


def convertBezToOutline(ufoFontData, glyphName, bezString):
    """
    Since the UFO outline element as no attributes to preserve,
    I can just make a new one.
    """
    # convert bez data to a UFO glif XML representation
    #
    # Convert all bez ops to simplest UFO equivalent
    # Add all hints to vertical and horizontal hint lists as encountered;
    # insert a HintMask class whenever a new set of hints is encountered
    # after all operators have been processed, convert HintMask items into
    # hintmask ops and hintmask bytes add all hints as prefix review operator
    # list to optimize T2 operators.
    # if useStem3 == 1, then any counter hints must be processed as stem3
    # hints, else the opposite.
    # Counter hints are used only in LanguageGroup 1 glyphs, aka ideographs

    bezString = re.sub(r"%.+?\n", "", bezString)  # supress comments
    bezList = re.findall(r"(\S+)", bezString)
    if not bezList:
        return "", None, None
    flexList = []
    # Create an initial hint mask. We use this if
    # there is no explicit initial hint sub.
    hintMask = HintMask(0)
    hintMaskList = [hintMask]
    vStem3Args = []
    hStem3Args = []
    argList = []
    opList = []
    newHintMaskName = None
    inPreFlex = False
    hintInfoDict = None
    opIndex = 0
    curX = 0
    curY = 0
    newOutline = XMLElement("outline")
    outlineItem = None
    seenHints = False

    for token in bezList:
        try:
            val = float(token)
            argList.append(val)
            continue
        except ValueError:
            pass
        if token == "newcolors":
            pass
        elif token in ["beginsubr", "endsubr"]:
            pass
        elif token in ["snc"]:
            hintMask = HintMask(opIndex)
            # If the new hints precedes any marking operator,
            # then we want throw away the initial hint mask we
            # made, and use the new one as the first hint mask.
            if opIndex == 0:
                hintMaskList = [hintMask]
            else:
                hintMaskList.append(hintMask)
            newHintMaskName = hintMask.pointName
        elif token in ["enc"]:
            pass
        elif token == "div":
            value = argList[-2] / float(argList[-1])
            argList[-2:] = [value]
        elif token == "rb":
            if newHintMaskName is None:
                newHintMaskName = hintMask.pointName
            hintMask.hList.append(argList)
            argList = []
            seenHints = True
        elif token == "ry":
            if newHintMaskName is None:
                newHintMaskName = hintMask.pointName
            hintMask.vList.append(argList)
            argList = []
            seenHints = True
        elif token == "rm":  # vstem3's are vhints
            if newHintMaskName is None:
                newHintMaskName = hintMask.pointName
            seenHints = True
            vStem3Args.append(argList)
            argList = []
            if len(vStem3Args) == 3:
                hintMask.vstem3List.append(vStem3Args)
                vStem3Args = []

        elif token == "rv":  # hstem3's are hhints
            seenHints = True
            hStem3Args.append(argList)
            argList = []
            if len(hStem3Args) == 3:
                hintMask.hstem3List.append(hStem3Args)
                hStem3Args = []

        elif token == "preflx1":
            # the preflx1/preflx2a sequence provides the same i as the flex
            # sequence; the difference is that the preflx1/preflx2a sequence
            # provides the argument values needed for building a Type1 string
            # while the flex sequence is simply the 6 rcurveto points. Both
            # sequences are always provided.
            argList = []
            # need to skip all move-tos until we see the "flex" operator.
            inPreFlex = True
        elif token == "preflx2a":
            argList = []
        elif token == "flxa":  # flex with absolute coords.
            inPreFlex = False
            flexPointName = kBaseFlexName + str(opIndex).zfill(4)
            flexList.append(flexPointName)
            curveCnt = 2
            i = 0
            # The first 12 args are the 6 args for each of
            # the two curves that make up the flex feature.
            while i < curveCnt:
                curX = argList[0]
                curY = argList[1]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY})
                outlineItem.append(newPoint)
                curX = argList[2]
                curY = argList[3]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY})
                outlineItem.append(newPoint)
                curX = argList[4]
                curY = argList[5]
                showX, showY = convertCoords(curX, curY)
                opName = 'curve'
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY,
                              "type": opName})
                outlineItem.append(newPoint)
                opList.append([opName, curX, curY])
                opIndex += 1
                if i == 0:
                    argList = argList[6:12]
                i += 1
            # attach the point name to the first point of the first curve.
            outlineItem[-6].set(kPointName, flexPointName)
            if newHintMaskName is not None:
                # We have a hint mask that we want to attach to the first
                # point of the flex op. However, there is already a flex
                # name in that attribute. What we do is set the flex point
                # name into the hint mask.
                hintMask.pointName = flexPointName
                newHintMaskName = None
            argList = []
        elif token == "sc":
            pass
        elif token == "cp":
            pass
        elif token == "ed":
            pass
        else:
            if inPreFlex and token in ["rmt", "mt"]:
                continue

            if token in ["rmt", "mt", "dt", "ct"]:
                opIndex += 1
            else:
                raise BezParseError(
                    "Unhandled operation: '%s' '%s'." % (argList, token))
            opName = bezToUFOPoint[token]
            if token in ["rmt", "mt", "dt"]:
                if token in ["mt", "dt"]:
                    curX = argList[0]
                    curY = argList[1]
                else:
                    curX += argList[0]
                    curY += argList[1]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY,
                              "type": "%s" % opName})

                if opName == "move":
                    if outlineItem is not None:
                        if len(outlineItem) == 1:
                            # Just in case we see two moves in a row,
                            # delete the previous outlineItem if it has
                            # only the move-to
                            log.info("Deleting moveto: %s adding %s",
                                     xmlToString(newOutline[-1]),
                                     xmlToString(outlineItem))
                            del newOutline[-1]
                        else:
                            # Fix the start/implied end path
                            # of the previous path.
                            fixStartPoint(outlineItem, opList)
                    opList = []
                    outlineItem = XMLElement('contour')
                    newOutline.append(outlineItem)

                if newHintMaskName is not None:
                    newPoint.set(kPointName, newHintMaskName)
                    newHintMaskName = None
                outlineItem.append(newPoint)
                opList.append([opName, curX, curY])
            else:  # "ct"
                curX = argList[0]
                curY = argList[1]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY})
                outlineItem.append(newPoint)
                curX = argList[2]
                curY = argList[3]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY})
                outlineItem.append(newPoint)
                curX = argList[4]
                curY = argList[5]
                showX, showY = convertCoords(curX, curY)
                newPoint = XMLElement(
                    "point", {"x": "%s" % showX, "y": "%s" % showY,
                              "type": "%s" % opName})
                outlineItem.append(newPoint)
                if newHintMaskName is not None:
                    # attach the pointName to the first point of the curve.
                    outlineItem[-3].set(kPointName, newHintMaskName)
                    newHintMaskName = None
                opList.append([opName, curX, curY])
            argList = []

    if outlineItem is not None:
        if len(outlineItem) == 1:
            # Just in case we see two moves in a row, delete
            # the previous outlineItem if it has zero length.
            del newOutline[-1]
        else:
            fixStartPoint(outlineItem, opList)

    # add hints, if any
    # Must be done at the end of op processing to make sure we have seen
    # all the hints in the bez string.
    # Note that the hintmasks are identified in the opList by the point name.
    # We will follow the T1 spec: a glyph may have stem3 counter hints or
    # regular hints, but not both.

    if (seenHints) or (len(flexList) > 0):
        hintInfoDict = XMLElement("dict")

        idItem = XMLElement("key")
        idItem.text = "id"
        hintInfoDict.append(idItem)

        idString = XMLElement("string")
        idString.text = "id"
        hintInfoDict.append(idString)

        hintSetListItem = XMLElement("key")
        hintSetListItem.text = kHintSetListName
        hintInfoDict.append(hintSetListItem)

        hintSetListArray = XMLElement("array")
        hintInfoDict.append(hintSetListArray)
        # Convert the rest of the hint masks
        # to a hintmask op and hintmask bytes.
        for hintMask in hintMaskList:
            hintMask.addHintSet(hintSetListArray)

        if len(flexList) > 0:
            hintSetListItem = XMLElement("key")
            hintSetListItem.text = kFlexIndexListName
            hintInfoDict.append(hintSetListItem)

            flexArray = XMLElement("array")
            hintInfoDict.append(flexArray)
            addFlexHint(flexList, flexArray)

    return newOutline, hintInfoDict


def addHintList(hints, hintsStem3, newHintSetArray, isH):
    # A charstring may have regular v stem hints or vstem3 hints, but not both.
    # Same for h stem hints and hstem3 hints.
    if len(hintsStem3) > 0:
        hintsStem3.sort()
        numHints = len(hintsStem3)
        hintLimit = int((kStackLimit - 2) / 2)
        if numHints >= hintLimit:
            hintsStem3 = hintsStem3[:hintLimit]
            numHints = hintLimit
        makeStemHintList(hintsStem3, newHintSetArray, isH)

    else:
        hints.sort()
        numHints = len(hints)
        hintLimit = int((kStackLimit - 2) / 2)
        if numHints >= hintLimit:
            hints = hints[:hintLimit]
            numHints = hintLimit
        makeHintList(hints, newHintSetArray, isH)


def addWhiteSpace(parent, level):
    child = None
    childIndent = "\n" + ("  " * (level + 1))
    prentIndent = "\n" + ("  " * (level))
    for child in parent:
        child.tail = childIndent
        addWhiteSpace(child, level + 1)
    if child is not None:
        if parent.text is None:
            parent.text = childIndent
        child.tail = prentIndent


def convertBezToGLIF(ufoFontData, glyphName, bezString, hintsOnly=False):
    # I need to replace the contours with data from the bez string.
    glyphPath = ufoFontData.getGlyphSrcPath(glyphName)
    glifXML = ET.parse(glyphPath).getroot()

    outlineItem = None
    libIndex = outlineIndex = -1
    outlineIndex = outlineIndex = -1
    childIndex = 0
    for childElement in glifXML:
        if childElement.tag == "outline":
            outlineItem = childElement
            outlineIndex = childIndex
        if childElement.tag == "lib":
            libIndex = childIndex
        childIndex += 1

    newOutlineElement, hintInfoDict = convertBezToOutline(
        ufoFontData, glyphName, bezString)

    if not hintsOnly:
        if outlineItem is None:
            # need to add it. Add it before the lib item, if any.
            if libIndex > 0:
                glifXML.insert(libIndex, newOutlineElement)
            else:
                glifXML.append(newOutlineElement)
        else:
            # remove the old one and add the new one.
            glifXML.remove(outlineItem)
            glifXML.insert(outlineIndex, newOutlineElement)

    # convertBezToGLIF is called only if the GLIF has been edited by a tool.
    # We need to update the edit status in the has map entry. I assume that
    # convertGLIFToBez has ben run before, which will add an entry for this
    # glyph.
    ufoFontData.updateHashEntry(glyphName, changed=True)
    # Add the stem hints.
    if hintInfoDict is not None:
        widthXML = glifXML.find("advance")
        if widthXML is not None:
            width = int(widthXML.get("width"))
        else:
            width = 1000
        useDefaultGlyphDir = False
        newGlyphHash, _ = ufoFontData.buildGlyphHashValue(
            width, newOutlineElement, glyphName, useDefaultGlyphDir)
        # We add this hash to the T1 data, as it is the hash which matches
        # the output outline data. This is not necessarily the same as the
        # hash of the source data - autohint can be used to change outlines.
        if libIndex > 0:
            libItem = glifXML[libIndex]
        else:
            libItem = XMLElement("lib")
            glifXML.append(libItem)

        dictItem = libItem.find("dict")
        if dictItem is None:
            dictItem = XMLElement("dict")
            libItem.append(dictItem)

        # Remove any existing hint data.
        i = 0
        childList = list(dictItem)
        for childItem in childList:
            i += 1
            if (childItem.tag == "key") and (
                (childItem.text == kHintDomainName1) or (
                    childItem.text == kHintDomainName2)):
                dictItem.remove(childItem)  # remove key
                dictItem.remove(childList[i])  # remove data item.

        glyphDictItem = dictItem
        key = XMLElement("key")
        key.text = kHintDomainName2
        glyphDictItem.append(key)

        glyphDictItem.append(hintInfoDict)

        childList = list(hintInfoDict)
        idValue = childList[1]
        idValue.text = newGlyphHash

    addWhiteSpace(glifXML, 0)
    return glifXML
