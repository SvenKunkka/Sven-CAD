#import <Foundation/Foundation.h>
#import <ApplicationServices/ApplicationServices.h>

static id readAX(AXUIElementRef element, CFStringRef attribute)
{
    CFTypeRef value = NULL;
    if (AXUIElementCopyAttributeValue(element, attribute, &value) != kAXErrorSuccess)
        return nil;
    return CFBridgingRelease(value);
}
static void dump(AXUIElementRef element, int depth)
{
    NSMutableDictionary *result = [NSMutableDictionary dictionary];
    for (NSString *key in @[@"AXRole", @"AXTitle", @"AXDescription", @"AXValue", @"AXLabel", @"AXIdentifier", @"AXSelected"]) {
        id value = readAX(element, (__bridge CFStringRef)key);
        if ([value isKindOfClass:NSString.class] || [value isKindOfClass:NSNumber.class])
            result[key] = value;
    }
    if (depth >= 4) {
        NSLog(@"%d %@", depth, result);
        return;
    }
    NSArray *children = readAX(element, kAXChildrenAttribute);
    result[@"children"] = @(children.count);
    NSLog(@"%d %@", depth, result);
    for (id child in children)
        dump((__bridge AXUIElementRef)child, depth + 1);
}
int main(int argc, char **argv)
{
    @autoreleasepool {
        if (argc != 2 || !AXIsProcessTrusted()) return 1;
        AXUIElementRef application = AXUIElementCreateApplication(atoi(argv[1]));
        NSMutableArray *queue = [NSMutableArray arrayWithArray:readAX(application, kAXWindowsAttribute) ?: @[]];
        for (NSUInteger index = 0; index < queue.count && index < 150; ++index) {
            AXUIElementRef element = (__bridge AXUIElementRef)queue[index];
            if ([@"PersonalCadAxProbeModelTree" isEqual:readAX(element, kAXIdentifierAttribute)]) {
                NSArray *selected = readAX(element, kAXSelectedChildrenAttribute);
                NSLog(@"selected=%lu", (unsigned long)selected.count);
                for (id child in selected)
                    dump((__bridge AXUIElementRef)child, 0);
                CFRelease(application);
                return 0;
            }
            [queue addObjectsFromArray:readAX(element, kAXChildrenAttribute) ?: @[]];
        }
        CFRelease(application);
        return 2;
    }
}
