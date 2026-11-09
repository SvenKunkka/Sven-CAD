#import <AppKit/AppKit.h>

@interface ProbeTable : NSTableView
@end

@implementation ProbeTable
- (NSArray *)accessibilitySelectedChildren
{
    return self.accessibilitySelectedRows ?: @[];
}
@end

@interface Rows : NSObject <NSTableViewDataSource, NSTableViewDelegate>
@end

@implementation Rows
- (NSInteger)numberOfRowsInTableView:(NSTableView *)table
{
    (void)table;
    return 2;
}

- (NSView *)tableView:(NSTableView *)table viewForTableColumn:(NSTableColumn *)column row:(NSInteger)row
{
    (void)table;
    (void)column;
    return [NSTextField labelWithString:row == 0 ? @"AX Probe Sentinel" : @"AX Probe Neighbor"];
}
@end

int main(int argc, const char *argv[])
{
    @autoreleasepool {
        NSApplication *app = NSApplication.sharedApplication;
        [app setActivationPolicy:NSApplicationActivationPolicyAccessory];
        NSWindow *window = [[NSWindow alloc] initWithContentRect:NSMakeRect(30, 60, 320, 100)
                                                      styleMask:NSWindowStyleMaskTitled
                                                        backing:NSBackingStoreBuffered defer:NO];
        window.title = @"Personal CAD AX QA fixture";
        ProbeTable *table = [[ProbeTable alloc] initWithFrame:window.contentView.bounds];
        table.accessibilityIdentifier = @"AXProbeFixture";
        [table addTableColumn:[[NSTableColumn alloc] initWithIdentifier:@"fixture"]];
        __attribute__((objc_precise_lifetime)) Rows *rows = [Rows new];
        table.dataSource = rows;
        table.delegate = rows;
        table.allowsEmptySelection = YES;
        window.contentView = table;
        [table reloadData];
        if (argc < 2 || strcmp(argv[1], "--empty") != 0) {
            [table selectRowIndexes:[NSIndexSet indexSetWithIndex:0] byExtendingSelection:NO];
        }
        [window orderFront:nil];
        dispatch_async(dispatch_get_main_queue(), ^{
            puts("ready");
            fflush(stdout);
        });
        [app run];
        return 0;
    }
}
