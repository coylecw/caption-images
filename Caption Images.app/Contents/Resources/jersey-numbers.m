// jersey-numbers: reads the jersey numbers in a sports photo with Apple Vision.
// Everything runs on this Mac; nothing is sent anywhere, and no face data is kept.
//
//   1. find the players (body pose, which works from behind too, plus faces)
//   2. read a zoomed-in crop of each player's jersey
//   3. sweep the whole photo in overlapping tiles for anything missed
//   4. group the reads of the same spot and let them vote
//
// Usage: jersey-numbers [options] photo.jpg [...]
//   --crops DIR   also save each player's jersey (torso) as DIR/<name>_t<i>.jpg
//   --name NAME   the <name> part (default: the photo's file name)
//   --fast        jerseys only: skip the sweep of the whole photo
//   --model M     a compiled number model (.mlmodelc) trained on your own jerseys
//                 ("Teach numbers"); each jersey gets its guess too
// Prints one JSON line per photo:
//   {"file":..., "numbers":[{"n":"23", "alts":["28"], "votes":3.3, "onPlayer":true,
//                            "color":[r,g,b], "box":[x,y,w,h]}]}
// "alts" are other ways the reader saw the same digits. "onPlayer" is false for
// numbers not on a player (scoreboards, banners). Boxes are fractions, top-left origin.
// With --model, "model" is how sure the model is of the same number on that jersey;
// a number only the model saw has "votes": 0. "torso" names the saved jersey crop.
#import <Foundation/Foundation.h>
#import <CoreML/CoreML.h>
#import <Vision/Vision.h>
#import <ImageIO/ImageIO.h>
#import <CoreGraphics/CoreGraphics.h>

// crop (pixels, top-left origin), scaled up so the short side is at least minSide
static CGImageRef cropScaled(CGImageRef img, CGRect r, CGFloat minSide, CGFloat maxScale) {
    CGRect full = CGRectMake(0, 0, CGImageGetWidth(img), CGImageGetHeight(img));
    r = CGRectIntegral(CGRectIntersection(r, full));
    if (r.size.width < 16 || r.size.height < 16) return NULL;
    CGImageRef crop = CGImageCreateWithImageInRect(img, r);
    if (!crop) return NULL;
    CGFloat s = MIN(maxScale, MAX(1.0, minSide / MIN(r.size.width, r.size.height)));
    if (s <= 1.01) return crop;
    size_t w = (size_t)(r.size.width * s), h = (size_t)(r.size.height * s);
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate(NULL, w, h, 8, 0, cs, (CGBitmapInfo)kCGImageAlphaNoneSkipLast);
    CGColorSpaceRelease(cs);
    if (!ctx) return crop;
    CGContextSetInterpolationQuality(ctx, kCGInterpolationHigh);
    CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), crop);
    CGImageRelease(crop);
    CGImageRef out = CGBitmapContextCreateImage(ctx);
    CGContextRelease(ctx);
    return out;
}

// crop scaled (up or down) so its short side is `side` px
static CGImageRef cropSized(CGImageRef img, CGRect r, CGFloat side) {
    CGRect full = CGRectMake(0, 0, CGImageGetWidth(img), CGImageGetHeight(img));
    r = CGRectIntegral(CGRectIntersection(r, full));
    if (r.size.width < 16 || r.size.height < 16) return NULL;
    CGImageRef crop = CGImageCreateWithImageInRect(img, r);
    if (!crop) return NULL;
    CGFloat s = side / MIN(r.size.width, r.size.height);
    size_t w = MAX(1, (size_t)(r.size.width * s)), h = MAX(1, (size_t)(r.size.height * s));
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate(NULL, w, h, 8, 0, cs, (CGBitmapInfo)kCGImageAlphaNoneSkipLast);
    CGColorSpaceRelease(cs);
    if (!ctx) return crop;
    CGContextSetInterpolationQuality(ctx, kCGInterpolationHigh);
    CGContextDrawImage(ctx, CGRectMake(0, 0, w, h), crop);
    CGImageRelease(crop);
    CGImageRef out = CGBitmapContextCreateImage(ctx);
    CGContextRelease(ctx);
    return out;
}

static BOOL saveJPEG(CGImageRef img, NSString *path) {
    CGImageDestinationRef d = CGImageDestinationCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:path], CFSTR("public.jpeg"), 1, NULL);
    if (!d) return NO;
    CGImageDestinationAddImage(d, img, (__bridge CFDictionaryRef)@{(id)kCGImageDestinationLossyCompressionQuality: @0.85});
    BOOL ok = CGImageDestinationFinalize(d);
    CFRelease(d);
    return ok;
}

// average colour of a region, 0-255
static NSArray *averageColor(CGImageRef img, CGRect r) {
    CGRect full = CGRectMake(0, 0, CGImageGetWidth(img), CGImageGetHeight(img));
    r = CGRectIntegral(CGRectIntersection(r, full));
    if (r.size.width < 4 || r.size.height < 4) return nil;
    CGImageRef crop = CGImageCreateWithImageInRect(img, r);
    if (!crop) return nil;
    unsigned char px[16 * 16 * 4];
    CGColorSpaceRef cs = CGColorSpaceCreateDeviceRGB();
    CGContextRef ctx = CGBitmapContextCreate(px, 16, 16, 8, 64, cs, (CGBitmapInfo)kCGImageAlphaNoneSkipLast);
    CGColorSpaceRelease(cs);
    CGContextDrawImage(ctx, CGRectMake(0, 0, 16, 16), crop);
    CGContextRelease(ctx);
    CGImageRelease(crop);
    double t[3] = {0, 0, 0};
    for (int i = 0; i < 256; i++) for (int c = 0; c < 3; c++) t[c] += px[i * 4 + c];
    return @[@((int)(t[0] / 256)), @((int)(t[1] / 256)), @((int)(t[2] / 256))];
}

// ---- reads -------------------------------------------------------------------

@interface Read : NSObject
@property CGRect box;                 // pixels, top-left origin
@property NSMutableDictionary *votes; // "23" -> weight
@property BOOL fromPlayer;            // read on a player's jersey crop
@end
@implementation Read
@end

static NSString *best(NSDictionary *votes) {
    NSString *b = nil;
    for (NSString *k in votes) if (!b || [votes[k] doubleValue] > [votes[b] doubleValue]) b = k;
    return b;
}

// every 1-2 digit number in img; boxes mapped back into the photo through `region`
static void readNumbers(CGImageRef img, CGRect region, BOOL fromPlayer, NSMutableArray<Read *> *reads) {
    VNRecognizeTextRequest *req = [VNRecognizeTextRequest new];
    req.recognitionLevel = VNRequestTextRecognitionLevelAccurate;
    req.usesLanguageCorrection = NO;
    if (![[[VNImageRequestHandler alloc] initWithCGImage:img options:@{}] performRequests:@[req] error:nil]) return;
    NSCharacterSet *digits = [NSCharacterSet characterSetWithCharactersInString:@"0123456789"];
    for (VNRecognizedTextObservation *obs in req.results) {
        NSMutableDictionary *votes = [NSMutableDictionary dictionary];
        double w = 1.0;
        for (VNRecognizedText *cand in [obs topCandidates:3]) {
            NSString *s = [[cand.string stringByReplacingOccurrencesOfString:@"#" withString:@""]
                           stringByTrimmingCharactersInSet:NSCharacterSet.whitespaceAndNewlineCharacterSet];
            if (s.length >= 1 && s.length <= 2 && [[s stringByTrimmingCharactersInSet:digits] length] == 0) {
                NSString *n = [NSString stringWithFormat:@"%d", s.intValue];
                if (!votes[n]) votes[n] = @(w * MAX(cand.confidence, 0.3));
            }
            w = 0.35;   // later guesses count for less
        }
        if (!votes.count) continue;
        CGRect b = obs.boundingBox;   // normalized, bottom-left origin, within the crop
        Read *r = [Read new];
        r.box = CGRectMake(region.origin.x + b.origin.x * region.size.width,
                           region.origin.y + (1 - b.origin.y - b.size.height) * region.size.height,
                           b.size.width * region.size.width, b.size.height * region.size.height);
        r.votes = votes;
        r.fromPlayer = fromPlayer;
        [reads addObject:r];
    }
}

static BOOL sameSpot(CGRect a, CGRect b) {
    CGRect i = CGRectIntersection(a, b);
    if (CGRectIsNull(i)) return NO;
    CGFloat ia = i.size.width * i.size.height;
    return ia > 0.3 * MIN(a.size.width * a.size.height, b.size.width * b.size.height);
}

// ---- options ---------------------------------------------------------------------

static NSString *CROPS = nil, *NAME = nil;
static BOOL FAST = NO;
static VNCoreMLModel *MODEL = nil;

// the model's best guess for one jersey crop
static NSString *classify(CGImageRef crop, double *conf) {
    *conf = 0;
    if (!MODEL) return nil;
    VNCoreMLRequest *req = [[VNCoreMLRequest alloc] initWithModel:MODEL];
    req.imageCropAndScaleOption = VNImageCropAndScaleOptionScaleFit;
    if (![[[VNImageRequestHandler alloc] initWithCGImage:crop options:@{}] performRequests:@[req] error:nil]) return nil;
    VNClassificationObservation *top = nil;
    for (VNClassificationObservation *o in req.results)
        if ([o isKindOfClass:VNClassificationObservation.class] && (!top || o.confidence > top.confidence)) top = o;
    if (!top) return nil;
    *conf = top.confidence;
    return top.identifier;
}

// ---- one photo -----------------------------------------------------------------

static NSDictionary *process(NSString *path) {
    CGImageSourceRef src = CGImageSourceCreateWithURL((__bridge CFURLRef)[NSURL fileURLWithPath:path], NULL);
    if (!src) return @{@"file": path, @"error": @"unreadable"};
    NSDictionary *opts = @{(id)kCGImageSourceCreateThumbnailFromImageAlways: @YES,
                           (id)kCGImageSourceCreateThumbnailWithTransform: @YES,   // upright
                           (id)kCGImageSourceThumbnailMaxPixelSize: @6000};
    CGImageRef img = CGImageSourceCreateThumbnailAtIndex(src, 0, (__bridge CFDictionaryRef)opts);
    CFRelease(src);
    if (!img) return @{@"file": path, @"error": @"unreadable"};
    CGFloat W = CGImageGetWidth(img), H = CGImageGetHeight(img);
    CGRect whole = CGRectMake(0, 0, W, H);

    // 1. players: body pose (front or back) and faces
    VNDetectHumanBodyPoseRequest *pose = [VNDetectHumanBodyPoseRequest new];
    VNDetectFaceRectanglesRequest *faces = [VNDetectFaceRectanglesRequest new];
    VNDetectHumanRectanglesRequest *bodies = [VNDetectHumanRectanglesRequest new];
    [[[VNImageRequestHandler alloc] initWithCGImage:img options:@{}] performRequests:@[pose, faces, bodies] error:nil];

    NSMutableArray<NSValue *> *torsos = [NSMutableArray array];
    for (VNHumanBodyPoseObservation *p in pose.results) {
        NSDictionary *pts = [p recognizedPointsForGroupKey:VNRecognizedPointGroupKeyAll error:nil];
        CGPoint ls = CGPointZero, rs = CGPointZero, lh = CGPointZero, rh = CGPointZero;
        BOOL hasL = NO, hasR = NO, hasLH = NO, hasRH = NO;
        VNRecognizedPoint *q;
#define PT(key, var, flag) q = pts[key]; if (q && q.confidence > 0.25) { var = CGPointMake(q.location.x * W, (1 - q.location.y) * H); flag = YES; }
        PT(VNHumanBodyPoseObservationJointNameLeftShoulder, ls, hasL)
        PT(VNHumanBodyPoseObservationJointNameRightShoulder, rs, hasR)
        PT(VNHumanBodyPoseObservationJointNameLeftHip, lh, hasLH)
        PT(VNHumanBodyPoseObservationJointNameRightHip, rh, hasRH)
        if (!hasL && !hasR) continue;
        CGPoint a = hasL ? ls : rs, b = hasR ? rs : ls;
        CGFloat sw = MAX(fabs(a.x - b.x), 1), top = MIN(a.y, b.y);
        CGFloat bottom = (hasLH || hasRH) ? MAX(hasLH ? lh.y : 0, hasRH ? rh.y : 0) : top + sw * 1.5;
        CGFloat th = MAX(bottom - top, sw * 0.8);
        CGFloat cx = (a.x + b.x) / 2, w = MAX(sw * 1.5, th * 0.85);
        [torsos addObject:[NSValue valueWithRect:NSRectFromCGRect(CGRectMake(cx - w / 2, top - th * 0.1, w, th * 1.2))]];
    }
    for (VNFaceObservation *f in faces.results) {   // the jersey is below the chin
        CGRect fr = CGRectMake(f.boundingBox.origin.x * W, (1 - f.boundingBox.origin.y - f.boundingBox.size.height) * H,
                               f.boundingBox.size.width * W, f.boundingBox.size.height * H);
        if (fr.size.height < H * 0.02) continue;
        CGRect t = CGRectMake(CGRectGetMidX(fr) - fr.size.width * 1.8, CGRectGetMaxY(fr) + fr.size.height * 0.3, fr.size.width * 3.6, fr.size.height * 3.5);
        BOOL known = NO;
        for (NSValue *v in torsos) if (sameSpot(NSRectToCGRect(v.rectValue), t)) { known = YES; break; }
        if (!known) [torsos addObject:[NSValue valueWithRect:NSRectFromCGRect(t)]];
    }
    // anywhere a player is: torsos plus Vision's person boxes
    NSMutableArray<NSValue *> *playerAreas = [NSMutableArray arrayWithArray:torsos];
    for (VNDetectedObjectObservation *h in bodies.results) {
        CGRect b = h.boundingBox;
        [playerAreas addObject:[NSValue valueWithRect:NSRectFromCGRect(CGRectMake(b.origin.x * W, (1 - b.origin.y - b.size.height) * H, b.size.width * W, b.size.height * H))]];
    }

    // 2. each player's jersey, zoomed in
    NSMutableArray<Read *> *reads = [NSMutableArray array];
    NSUInteger n = 0;
    for (NSValue *v in torsos) {
        if (n++ >= 16) break;
        @autoreleasepool {
            CGRect t = CGRectIntersection(NSRectToCGRect(v.rectValue), whole);
            CGImageRef crop = cropScaled(img, t, 700, 4);
            if (crop) { readNumbers(crop, t, YES, reads); CGImageRelease(crop); }
        }
    }
    // 3. the whole photo in overlapping tiles
    NSMutableArray *tiles = [NSMutableArray arrayWithObject:[NSValue valueWithRect:NSRectFromCGRect(whole)]];
    int grids[2][2] = {{3, 2}, {4, 3}};
    for (int g = 0; g < 2; g++) {
        CGFloat tw = W / grids[g][0], th = H / grids[g][1];
        for (int i = 0; i < grids[g][0]; i++) for (int j = 0; j < grids[g][1]; j++)
            [tiles addObject:[NSValue valueWithRect:NSMakeRect(i * tw - tw * .25, j * th - th * .25, tw * 1.5, th * 1.5)]];
    }
    if (FAST) [tiles removeAllObjects];
    for (NSValue *v in tiles) {
        @autoreleasepool {
            CGRect r = CGRectIntersection(NSRectToCGRect(v.rectValue), whole);
            CGImageRef crop = cropScaled(img, r, 0, 1);
            if (crop) { readNumbers(crop, r, NO, reads); CGImageRelease(crop); }
        }
    }

    // 4. group reads of the same spot; they vote
    NSMutableArray<Read *> *spots = [NSMutableArray array];
    for (Read *r in reads) {
        if (!r.fromPlayer && r.box.size.height < H * 0.012) continue;   // too small to be a jersey
        Read *into = nil;
        for (Read *s in spots) if (sameSpot(s.box, r.box)) { into = s; break; }
        if (!into) {
            into = [Read new]; into.box = r.box; into.votes = [NSMutableDictionary dictionary]; [spots addObject:into];
        } else {
            into.box = CGRectUnion(into.box, r.box);
        }
        into.fromPlayer = into.fromPlayer || r.fromPlayer;
        for (NSString *k in r.votes) into.votes[k] = @([into.votes[k] doubleValue] + [r.votes[k] doubleValue] * (r.fromPlayer ? 1.5 : 1.0));
    }
    // 5. each jersey on its own: saved for "Teach numbers", and the trained model's guess
    NSMutableArray *jerseys = [NSMutableArray array];   // {crop, box(px), read, model, conf}
    if (CROPS || MODEL) {
        NSString *stem = NAME ?: [[path lastPathComponent] stringByDeletingPathExtension];
        NSUInteger ti = 0;
        for (NSValue *v in torsos) {
            if (ti >= 16) break;
            @autoreleasepool {
                CGRect t = CGRectIntersection(NSRectToCGRect(v.rectValue), whole);
                NSUInteger idx = ti++;
                if (t.size.width < 60 || t.size.height < 60) continue;
                // the clear number whose spot is on this jersey
                NSString *label = nil; double bestv = 0;
                for (Read *sp in spots) {
                    NSString *top = best(sp.votes); double total = 0; for (NSString *k in sp.votes) total += [sp.votes[k] doubleValue];
                    double v0 = [sp.votes[top] doubleValue];
                    CGPoint c = CGPointMake(CGRectGetMidX(sp.box), CGRectGetMidY(sp.box));
                    if (CGRectContainsPoint(t, c) && v0 >= 1.5 && v0 / total >= 0.7 && sp.box.size.height > t.size.height * 0.08 && v0 > bestv) { bestv = v0; label = top; }
                }
                CGImageRef crop = cropSized(img, t, 360);
                if (!crop) continue;
                NSMutableDictionary *j = [NSMutableDictionary dictionaryWithDictionary:@{@"rect": [NSValue valueWithRect:NSRectFromCGRect(t)],
                    @"box": @[@(t.origin.x / W), @(t.origin.y / H), @(t.size.width / W), @(t.size.height / H)]}];
                if (label) j[@"read"] = label;
                if (CROPS) {
                    NSString *name = [NSString stringWithFormat:@"%@_t%lu.jpg", stem, (unsigned long)idx];
                    if (saveJPEG(crop, [CROPS stringByAppendingPathComponent:name])) j[@"crop"] = name;
                }
                double conf = 0;
                NSString *guess = classify(crop, &conf);
                if (guess) { j[@"model"] = guess; j[@"conf"] = @(round(conf * 100) / 100); }
                CGImageRelease(crop);
                [jerseys addObject:j];
            }
        }
    }
    // the jersey a number sits on
    NSDictionary *(^jerseyAt)(CGRect) = ^NSDictionary *(CGRect b) {
        CGPoint c = CGPointMake(CGRectGetMidX(b), CGRectGetMidY(b));
        for (NSDictionary *j in jerseys) if (CGRectContainsPoint(NSRectToCGRect([j[@"rect"] rectValue]), c)) return j;
        return nil;
    };

    NSMutableDictionary *byNumber = [NSMutableDictionary dictionary];
    for (Read *s in spots) {
        NSString *top = best(s.votes);
        double total = 0; for (NSString *k in s.votes) total += [s.votes[k] doubleValue];
        if ([s.votes[top] doubleValue] < 0.6) continue;   // one weak guess
        CGPoint c = CGPointMake(CGRectGetMidX(s.box), CGRectGetMidY(s.box));
        BOOL onPlayer = s.fromPlayer;
        for (NSValue *v in playerAreas) if (!onPlayer && CGRectContainsPoint(NSRectToCGRect(v.rectValue), c)) onPlayer = YES;
        NSMutableArray *alts = [NSMutableArray array];
        for (NSString *k in [s.votes keysSortedByValueUsingComparator:^(id a, id b) { return [b compare:a]; }])
            if (![k isEqual:top]) [alts addObject:k];
        NSMutableDictionary *o = [NSMutableDictionary dictionaryWithDictionary:@{
            @"n": top, @"alts": alts, @"votes": @(round([s.votes[top] doubleValue] * 100) / 100),
            @"agree": @(round([s.votes[top] doubleValue] / total * 100) / 100), @"onPlayer": @(onPlayer),
            @"box": @[@(s.box.origin.x / W), @(s.box.origin.y / H), @(s.box.size.width / W), @(s.box.size.height / H)]}];
        NSArray *col = averageColor(img, CGRectInset(s.box, -s.box.size.width * 0.6, -s.box.size.height * 0.15));
        if (col) o[@"color"] = col;
        NSDictionary *j = jerseyAt(s.box);
        if (j && onPlayer) {
            if (j[@"crop"]) o[@"torso"] = j[@"crop"];
            if ([j[@"model"] isEqual:top]) o[@"model"] = j[@"conf"];
        }
        NSDictionary *prev = byNumber[top];   // the same number twice: keep the stronger
        if (!prev || [prev[@"votes"] doubleValue] < [o[@"votes"] doubleValue]) byNumber[top] = o;
    }
    // numbers only the model saw: suggestions, never counted as a clear read (votes 0)
    for (NSDictionary *j in jerseys) {
        NSString *g = j[@"model"];
        if (!g || [g isEqual:@"none"] || [j[@"conf"] doubleValue] < 0.8 || byNumber[g]) continue;
        CGRect t = NSRectToCGRect([j[@"rect"] rectValue]);
        NSMutableDictionary *o = [NSMutableDictionary dictionaryWithDictionary:@{@"n": g, @"alts": @[], @"votes": @0, @"agree": @0,
            @"onPlayer": @YES, @"model": j[@"conf"], @"box": j[@"box"]}];
        if (j[@"crop"]) o[@"torso"] = j[@"crop"];
        NSArray *col = averageColor(img, CGRectMake(t.origin.x + t.size.width * 0.25, t.origin.y + t.size.height * 0.2, t.size.width * 0.5, t.size.height * 0.45));
        if (col) o[@"color"] = col;
        byNumber[g] = o;
    }
    NSMutableArray *jout = [NSMutableArray array];
    for (NSDictionary *j in jerseys) { NSMutableDictionary *m = [j mutableCopy]; [m removeObjectForKey:@"rect"]; [jout addObject:m]; }
    CGImageRelease(img);
    NSArray *nums = [byNumber.allValues sortedArrayUsingComparator:^NSComparisonResult(NSDictionary *a, NSDictionary *b) {
        if ([a[@"onPlayer"] boolValue] != [b[@"onPlayer"] boolValue]) return [a[@"onPlayer"] boolValue] ? NSOrderedAscending : NSOrderedDescending;
        return [b[@"votes"] compare:a[@"votes"]];
    }];
    return @{@"file": path, @"numbers": nums, @"jerseys": jout};
}

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        for (int i = 1; i < argc; i++) {
            if (!strcmp(argv[i], "--crops") && i + 1 < argc) { CROPS = @(argv[++i]); continue; }
            if (!strcmp(argv[i], "--name") && i + 1 < argc) { NAME = @(argv[++i]); continue; }
            if (!strcmp(argv[i], "--fast")) { FAST = YES; continue; }
            if (!strcmp(argv[i], "--model") && i + 1 < argc) {
                NSError *err = nil;
                MLModel *m = [MLModel modelWithContentsOfURL:[NSURL fileURLWithPath:@(argv[++i])] error:&err];
                if (m) MODEL = [VNCoreMLModel modelForMLModel:m error:&err];
                if (!MODEL) fprintf(stderr, "model not loaded: %s\n", err.localizedDescription.UTF8String);
                continue;
            }
            if (argv[i][0] == '-') continue;   // older callers passed --faces
            @autoreleasepool {
                NSData *d = [NSJSONSerialization dataWithJSONObject:process(@(argv[i])) options:0 error:nil];
                fwrite(d.bytes, 1, d.length, stdout); fputc('\n', stdout); fflush(stdout);
            }
        }
    }
    return 0;
}
